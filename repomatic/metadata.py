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

import ast
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import fields
from functools import cached_property, partial
from pathlib import Path

import tomlrt
from click_extra import field_docstrings
from extra_platforms import is_github_ci
from packaging.version import Version
from pyproject_metadata import ConfigurationError, StandardMetadata

from .binary import (
    BINARY_AFFECTING_PATHS,
    FLAT_BUILD_TARGETS,
    NUITKA_BUILD_TARGETS,
    SKIP_BINARY_BUILD_BRANCHES,
    binary_name,
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
    load_repomatic_config,
)
from .file_inventory import FileInventory
from .git_ops import (
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    RELEASE_COMMIT_PATTERN,
    SHORT_SHA_LENGTH,
    Commit,
    checkout,
    commit_exists,
    count_commits,
    current_branch,
    diff_names,
    fetch_deepen,
    get_commit,
    get_latest_tag_version,
    get_release_version_from_commits,
    get_repo_slug_from_remote,
    head_sha,
    list_commits,
    stash,
    stash_count,
    stash_pop,
)
from .github.actions import (
    NULL_SHA,
    WorkflowEvent,
    generate_delimiter,
    get_github_event,
)
from .github.gh import run_gh_command
from .github.matrix import Matrix, stale_axis_values
from .github.release_sync import build_expected_body
from .mailmap import MAILMAP_PATH
from .matrix_axes import (
    PRERELEASE_LABEL_SUFFIX,
    SINGLE_RUNNER_PYTHON_VERSIONS,
    TEST_PYTHON_FULL,
    TEST_PYTHON_PR,
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
    UNSTABLE_PYTHON_VERSIONS,
)
from .pypi import PYPI_PROJECT_URL
from .pyproject import (
    is_python_package as _is_python_package,
    is_python_project as _is_python_project,
)
from .registry import is_awesome_repo
from .tool_registry import MYPY_VERSION_MIN
from .version_sync import min_release_age_days

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any, Final, Literal

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

_SCRIPT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]+")
"""Allowed characters in a `[project.scripts]` entry name.

Matches the rule PyPI enforces on uploaded wheels and the validation
[uv-build performs](https://github.com/astral-sh/uv/pull/19495) before
writing wheel metadata. Names are also required to be non-empty and to
contain at least one non-dot character; both extra checks live next to
the regex in {meth}`Metadata.script_entries`.
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


def _known_build_targets(names: list[str], kind: str) -> set[str]:
    """Keep the recognized Nuitka build targets among *names*.

    Shared by {attr}`Metadata.dev_targets` and
    {attr}`Metadata.unstable_targets`, which read two different config lists
    against the same roster and would otherwise drift on how they treat a name
    that roster does not carry.

    :param names: Target names, as configured.
    :param kind: What the list configures, for the warning naming the strays.
    :return: The subset of *names* present in
        {data}`~repomatic.binary.NUITKA_BUILD_TARGETS`.
    """
    targets = set(names)
    unknown = targets - set(NUITKA_BUILD_TARGETS)
    if unknown:
        logging.warning(f"Unrecognized {kind} targets: {unknown}")
    return targets & set(NUITKA_BUILD_TARGETS)


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


def is_version_bump_allowed(part: Literal["minor", "major"]) -> bool:
    """Check if a version bump of the specified part is allowed.

    This prevents double version increments within a development cycle. A bump is
    blocked if the version has already been bumped (but not released) since the last
    tagged release.

    For example:
    - Last release: `v5.0.1`, current: `5.0.2` → minor bump allowed
    - Last release: `v5.0.1`, current: `5.1.0` → minor bump NOT allowed (bumped)
    - Last release: `v5.0.1`, current: `6.0.0` → major bump NOT allowed (bumped)

    ```{note}
    When tags are not available (e.g., due to race conditions between workflows),
    this function falls back to parsing version from recent commit messages.
    ```

    :param part: The version part to check (`minor` or `major`).
    :return: `True` if the bump should proceed, `False` if it should be skipped.
    """
    # Validate part argument early.
    if part not in ("minor", "major"):
        raise ValueError(f"Invalid version part: {part!r}. Must be 'minor' or 'major'.")

    current_version_str = Metadata.get_current_version()
    if not current_version_str:
        logging.warning("Cannot determine current version. Allowing bump.")
        return True

    # Try to get the latest release version from tags first.
    latest_release = get_latest_tag_version()

    # Fallback to commit message parsing if tag not found.
    # This handles race conditions where the release workflow hasn't pushed the tag yet.
    if not latest_release:
        logging.info("No tags found, falling back to commit message parsing.")
        latest_release = get_release_version_from_commits()

    if not latest_release:
        logging.warning("No release version found from tags or commits. Allowing bump.")
        return True

    current = Version(current_version_str)
    logging.info(f"Current version: {current}, Latest release: {latest_release}")

    if part == "major":
        # Block if major version is already ahead of the latest release.
        if current.major > latest_release.major:
            logging.info(
                "Major version already bumped "
                f"({current.major} > {latest_release.major}). Skipping bump."
            )
            return False
    elif part == "minor":
        # Block if major is ahead, or if minor is ahead within the same major.
        if current.major > latest_release.major:
            logging.info(
                "Major version already bumped "
                f"({current.major} > {latest_release.major}). Skipping minor bump."
            )
            return False
        if (
            current.major == latest_release.major
            and current.minor > latest_release.minor
        ):
            logging.info(
                "Minor version already bumped "
                f"({current.minor} > {latest_release.minor}). Skipping bump."
            )
            return False

    logging.info(f"Version bump for {part} is allowed.")
    return True


class JSONMetadata(json.JSONEncoder):
    """Custom JSON encoder for metadata serialization."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Matrix):
            return o.matrix()

        if isinstance(o, Path):
            return str(o)

        return super().default(o)


class Metadata:
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

    def git_stash_count(self) -> int:
        """Returns the number of stashes."""
        count = stash_count()
        logging.debug(f"Number of stashes in repository: {count}")
        return count

    def git_deepen(
        self, commit_hash: str, max_attempts: int = 10, deepen_increment: int = 50
    ) -> bool:
        """Deepen a shallow clone until the provided `commit_hash` is found.

        Progressively fetches more commits from the current repository until the
        specified commit is found or max attempts is reached.

        Returns `True` if the commit was found, `False` otherwise.
        """
        # Cache the current depth to avoid repeated subprocess calls.
        current_depth: int | None = None

        for attempt in range(max_attempts):
            if commit_exists(commit_hash):
                if attempt > 0:
                    logging.info(
                        f"Found commit {commit_hash} after {attempt} deepen "
                        "operation(s)."
                    )
                return True

            logging.debug(f"Commit {commit_hash} not found.")

            # Only compute depth if not cached yet.
            if current_depth is None:
                current_depth = count_commits()

            if attempt == max_attempts - 1:
                # We've exhausted all attempts.
                logging.error(
                    f"Cannot find commit {commit_hash} in repository after "
                    f"{max_attempts} deepen attempts. "
                    f"Final depth is {current_depth} commits."
                )
                return False

            logging.info(f"Commit {commit_hash} not found at depth {current_depth}.")
            logging.info(
                f"Deepening by {deepen_increment} commits (attempt "
                f"{attempt + 1}/{max_attempts})..."
            )

            try:
                fetch_deepen(deepen_increment)
            except subprocess.CalledProcessError as ex:
                logging.error(f"Failed to deepen repository: {ex}")
                return False
            # Update cached depth after successful fetch.
            current_depth = count_commits()
            logging.debug(
                f"Repository deepened successfully. New depth: {current_depth}"
            )

        return False

    def commit_matrix(self, commits: Sequence[Commit] | None) -> Matrix | None:
        """Pre-compute a matrix of commits.

        ```{danger}
        This method temporarily modify the state of the repository to compute
        version metadata from the past.

        To prevent any loss of uncommitted data, it stashes and unstash the
        local changes between checkouts.
        ```

        The list of commits is augmented with long and short SHA values, as well as
        current version. Most recent commit is first, oldest is last.

        Returns a ready-to-use matrix structure:

        ```{code-block} python
        {
            "commit": [
                "346ce664f055fbd042a25ee0b7e96702e95",
                "6f27db47612aaee06fdf08744b09a9f5f6c2",
            ],
            "include": [
                {
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "short_sha": "346ce66",
                    "current_version": "2.0.1",
                },
                {
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "short_sha": "6f27db4",
                    "current_version": "2.0.0",
                },
            ],
        }
        ```
        """
        if not commits:
            return None

        current_commit = head_sha()

        # Check if we need to get back in time in the Git log and browse past commits.
        if len(commits) == 1:
            # Is the current commit the one we're looking for?
            past_commit_lookup = bool(current_commit != commits[0].hash)
        # If we have multiple commits then yes, we need to look for past commits.
        else:
            past_commit_lookup = True

        # We need to go back in time, but first save the current state of the
        # repository.
        if past_commit_lookup:
            logging.debug(
                "We need to look into the commit history. Inspect the initial state "
                "of the repository."
            )

            if not is_github_ci():
                raise RuntimeError(
                    "Local repository manipulations only allowed in CI environment"
                )

            # Save the initial commit reference and SHA of the repository. The
            # reference is either the canonical active branch name (i.e. `main`), or
            # the commit SHA if the current HEAD commit is detached from a branch.
            init_ref = current_branch() or current_commit
            logging.debug(f"Initial commit reference: {init_ref}")

            # Try to stash local changes and check if we'll need to unstash them later.
            counter_before = self.git_stash_count()
            logging.debug("Try to stash local changes before our series of checkouts.")
            stash()
            counter_after = self.git_stash_count()
            logging.debug(
                "Stash counter changes after 'git stash' command: "
                f"{counter_before} -> {counter_after}"
            )
            assert counter_after >= counter_before
            need_unstash = bool(counter_after > counter_before)
            logging.debug(f"Need to unstash after checkouts: {need_unstash}")

        else:
            init_ref = None
            need_unstash = False
            logging.debug(
                "No need to look into the commit history: repository is already "
                f"checked out at {current_commit}"
            )

        matrix = Matrix()
        for commit in commits:
            if past_commit_lookup:
                logging.debug(f"Checkout to commit {commit.hash}")
                checkout(commit.hash)

            commit_metadata = {
                "commit": commit.hash,
                "short_sha": commit.hash[:SHORT_SHA_LENGTH],
            }

            logging.debug(f"Extract project version at commit {commit.hash}")
            current_version = Metadata.get_current_version()
            if current_version:
                commit_metadata["current_version"] = current_version

            matrix.add_variation("commit", [commit.hash])
            matrix.add_includes(commit_metadata)

        # Restore the repository to its initial state.
        if past_commit_lookup:
            # init_ref is always set to a ref string when past_commit_lookup is True.
            assert init_ref is not None
            logging.debug(f"Restore repository to {init_ref}.")
            checkout(init_ref)
            if need_unstash:
                logging.debug("Unstash local changes that were previously saved.")
                stash_pop()

        return matrix

    @cached_property
    def event_type(self) -> WorkflowEvent | None:
        """Returns the type of event that triggered the workflow run.

        Maps {attr}`event_name` (the `GITHUB_EVENT_NAME` variable, set by
        GitHub Actions on every run) onto its
        {class}`~repomatic.github.actions.WorkflowEvent` member, so `schedule`
        and `workflow_dispatch` runs resolve to their own event instead of
        falling in a `None` hole that nulls every commit matrix.

        ```{caution}
        When `GITHUB_EVENT_NAME` is absent or unrecognized, falls back on the
        historical heuristic: a non-empty `GITHUB_BASE_REF` means a pull
        request ([only set for pull request events](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#default-environment-variables)),
        a present-but-empty one means a push.
        ```
        """
        if not is_github_ci():
            logging.warning(
                "Cannot guess event type because we're not in a CI environment."
            )
            return None
        if self.event_name:
            try:
                return WorkflowEvent(self.event_name)
            except ValueError:
                logging.warning(
                    f"Unrecognized workflow event {self.event_name!r};"
                    " falling back on the GITHUB_BASE_REF heuristic."
                )
        if "GITHUB_BASE_REF" not in os.environ:
            logging.warning(
                "Cannot guess event type because no GITHUB_BASE_REF env var found."
            )
            return None

        if bool(os.environ.get("GITHUB_BASE_REF")):
            return WorkflowEvent.pull_request
        return WorkflowEvent.push

    @cached_property
    def event_actor(self) -> str | None:
        """Returns the GitHub login of the user that triggered the workflow run."""
        return os.environ.get("GITHUB_ACTOR") or None

    @cached_property
    def event_sender_type(self) -> str | None:
        """Returns the type of the user that triggered the workflow run."""
        sender_type = self.github_event.get("sender", {}).get("type")
        if not sender_type:
            return None
        assert isinstance(sender_type, str)
        return sender_type

    @cached_property
    def is_bot(self) -> bool:
        """Returns `True` if the workflow was triggered by a bot or automated process.

        This is useful to only run some jobs on human-triggered events. Or skip jobs
        triggered by bots to avoid infinite loops.

        The sender type covers every GitHub App, which is how Dependabot and
        Renovate author their pull requests today. The explicit login list is
        kept as a second signal for downstream repositories: `sender.type` is
        absent from the event payload outside `push` and `pull_request` (and
        empty when the payload cannot be read at all), and the login is then
        the only thing left to match on.

        The test is deliberately not `sender.type != "User"`, which would also
        classify an `Organization` sender as a bot.
        """
        return self.event_sender_type == "Bot" or self.event_actor in (
            "dependabot[bot]",
            "dependabot-preview[bot]",
        )

    @cached_property
    def head_branch(self) -> str | None:
        """Returns the head branch name for pull request events.

        For pull request events, this is the source branch name
        (e.g., `update-mailmap`). For push events, returns `None` since
        there's no head branch concept.

        The branch name is extracted from the `GITHUB_HEAD_REF` environment variable,
        which is [only set for pull request events](https://docs.github.com/en/actions/learn-github-actions/variables).
        """
        head_ref = os.environ.get("GITHUB_HEAD_REF")
        if head_ref:
            return head_ref
        return None

    @cached_property
    def event_name(self) -> str | None:
        """Returns the name of the event that triggered the workflow.

        Reads `GITHUB_EVENT_NAME`. This is the raw event name (`"push"`,
        `"pull_request"`, `"workflow_run"`), which {attr}`event_type` resolves
        to a {class}`~repomatic.github.actions.WorkflowEvent` member.
        """
        return os.environ.get("GITHUB_EVENT_NAME") or None

    @cached_property
    def job_name(self) -> str | None:
        """Returns the ID of the current job in the workflow.

        Reads `GITHUB_JOB`.
        """
        return os.environ.get("GITHUB_JOB") or None

    @cached_property
    def ref_name(self) -> str | None:
        """Returns the short ref name of the branch or tag.

        Reads `GITHUB_REF_NAME`.
        """
        return os.environ.get("GITHUB_REF_NAME") or None

    @cached_property
    def repo_name(self) -> str | None:
        """Returns the repository name without owner prefix.

        Derived from {attr}`repo_slug` by splitting on `/`.
        """
        slug = self.repo_slug
        return slug.split("/")[-1] if slug else None

    @cached_property
    def is_awesome(self) -> bool:
        """Whether this is an awesome-list repository.

        Detected by the `awesome-` prefix on the repository name.
        """
        name = self.repo_name
        return bool(name and is_awesome_repo(name))

    @cached_property
    def repo_owner(self) -> str | None:
        """Returns the repository owner.

        Reads `GITHUB_REPOSITORY_OWNER`, falling back to the owner
        component of {attr}`repo_slug`.
        """
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or None
        if not owner:
            slug = self.repo_slug
            if slug and "/" in slug:
                owner = slug.split("/")[0]
        return owner

    @cached_property
    def repo_slug(self) -> str | None:
        """Returns the `owner/name` slug for the current repository.

        Resolution order: `GITHUB_REPOSITORY` env var (CI), `gh repo view`
        (authenticated local), git remote URL parsing (offline fallback).
        """
        slug = os.environ.get("GITHUB_REPOSITORY") or None
        if not slug:
            try:
                slug = (
                    run_gh_command(
                        [
                            "repo",
                            "view",
                            "--json",
                            "nameWithOwner",
                            "--jq",
                            ".nameWithOwner",
                        ],
                    ).strip()
                    or None
                )
            except RuntimeError:
                logging.debug("Failed to detect repository slug via gh CLI.")
        if not slug:
            slug = get_repo_slug_from_remote()
            if slug:
                logging.debug("Detected repository slug from git remote: %s", slug)
        return slug

    @cached_property
    def repo_url(self) -> str | None:
        """Returns the full URL to the repository.

        Derived from {attr}`server_url` and {attr}`repo_slug`.
        """
        slug = self.repo_slug
        if slug:
            return f"{self.server_url}/{slug}"
        return None

    @cached_property
    def run_attempt(self) -> str | None:
        """Returns the run attempt number.

        Reads `GITHUB_RUN_ATTEMPT`.
        """
        return os.environ.get("GITHUB_RUN_ATTEMPT") or None

    @cached_property
    def run_id(self) -> str | None:
        """Returns the unique ID of the current workflow run.

        Reads `GITHUB_RUN_ID`.
        """
        return os.environ.get("GITHUB_RUN_ID") or None

    @cached_property
    def run_number(self) -> str | None:
        """Returns the run number for the current workflow.

        Reads `GITHUB_RUN_NUMBER`.
        """
        return os.environ.get("GITHUB_RUN_NUMBER") or None

    @cached_property
    def server_url(self) -> str:
        """Returns the GitHub server URL.

        Reads `GITHUB_SERVER_URL`, defaulting to `https://github.com`.
        """
        return os.environ.get("GITHUB_SERVER_URL") or "https://github.com"

    @cached_property
    def sha(self) -> str | None:
        """Returns the commit SHA that triggered the workflow.

        Reads `GITHUB_SHA`.
        """
        return os.environ.get("GITHUB_SHA") or None

    @cached_property
    def triggering_actor(self) -> str | None:
        """Returns the login of the user that initiated the workflow run.

        Reads `GITHUB_TRIGGERING_ACTOR`. This differs from
        {attr}`event_actor` (`GITHUB_ACTOR`) when a workflow is re-run by a
        different user.
        """
        return os.environ.get("GITHUB_TRIGGERING_ACTOR") or None

    @cached_property
    def workflow_ref(self) -> str | None:
        """Returns the full workflow reference.

        Reads `GITHUB_WORKFLOW_REF`. The format is
        `owner/repo/.github/workflows/name.yaml@refs/heads/branch`.
        """
        return os.environ.get("GITHUB_WORKFLOW_REF") or None

    @cached_property
    def changed_files(self) -> tuple[str, ...] | None:
        """Returns the list of files changed in the current event's commit range.

        Uses `git diff --name-only` between the start and end of the commit range.
        Returns `None` if no commit range is available (e.g., outside CI).
        """
        if not self.commit_range:
            return None
        start, end = self.commit_range
        if not start or not end:
            return None
        try:
            return diff_names(start, end)
        except subprocess.CalledProcessError as ex:
            detail = ex.stderr.strip() if ex.stderr else ex
            logging.warning(f"Failed to get changed files from git diff: {detail}")
            return None

    @cached_property
    def binary_affecting_paths(self) -> tuple[str, ...]:
        """Path prefixes that affect compiled binaries for this project.

        Combines the static {data}`BINARY_AFFECTING_PATHS` (common files like
        `pyproject.toml`, `uv.lock`, `tests/`) with project-specific source
        directories derived from `[project.scripts]` in `pyproject.toml`.

        For example, a project with `mpm = "meta_package_manager.__main__:main"`
        adds `meta_package_manager/` as an affecting path. This makes the check
        reusable across downstream repositories without hardcoding source directories.
        """
        # Derive top-level source package directories from script entry points.
        source_dirs: set[str] = set()
        for _cli_id, module_id, _callable_id in self.script_entries:
            # Extract top-level package: "meta_package_manager.__main__" →
            # "meta_package_manager/".
            top_package = module_id.split(".")[0]
            source_dirs.add(f"{top_package}/")
        return BINARY_AFFECTING_PATHS + tuple(sorted(source_dirs))

    @cached_property
    def head_commit_message(self) -> str:
        """Returns `github.event.head_commit.message` from the event payload.

        Set for `push` events. Empty string for events that do not carry a
        head commit (`pull_request`, `schedule`, `workflow_dispatch`).
        """
        head_commit = self.github_event.get("head_commit") or {}
        return head_commit.get("message") or ""

    @cached_property
    def yaml_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one YAML file.

        Lets per-job lint gates short-circuit on pushes / PRs that don't
        touch YAML. Falls back to "repo contains any YAML file" when the
        commit range is unavailable (`workflow_dispatch`), preserving the
        existing behavior of those manual runs.
        """
        if self.changed_files is None:
            return bool(self.yaml_files)
        return any(f.endswith((".yaml", ".yml")) for f in self.changed_files)

    @cached_property
    def zsh_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one Zsh file.

        Falls back to "repo contains any Zsh file" when the commit range
        is unavailable.
        """
        if self.changed_files is None:
            return bool(self.zsh_files)
        zsh_set = set(self.zsh_files)
        return any(f in zsh_set for f in self.changed_files)

    @cached_property
    def workflows_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one GitHub workflow file.

        Falls back to "repo contains any workflow file" when the commit
        range is unavailable.
        """
        if self.changed_files is None:
            return bool(self.workflow_files)
        wf_set = set(self.workflow_files)
        return any(f in wf_set for f in self.changed_files)

    @cached_property
    def skip_binary_build(self) -> bool:
        """Returns `True` if binary builds should be skipped for this event.

        Binary builds are expensive and time-consuming. This property identifies
        contexts where the changes cannot possibly affect compiled binaries,
        allowing workflows to skip Nuitka compilation jobs.

        Three mechanisms are checked:

        1. **Branch name** — PRs from known non-code branches (documentation,
           `.mailmap`, `.gitignore`, etc.) are skipped.
        2. **Version-bump commit** — Push events whose head commit is a
           user-initiated version bump (`Bump (major|minor) version to `)
           are skipped: the bump merge changes only version strings and
           `uv.lock`, so the new binary differs from the previous one only
           in the baked-in version string. The
           `[changelog] Post-release bump ` prefix is deliberately *not*
           checked here: the `prepare-release` merge bundles the release
           commit with the post-release-bump commit, and the release
           commit must still produce its binary.
        3. **Changed files** — Push events where all changed files fall outside
           {attr}`binary_affecting_paths` are skipped. This avoids ~2h of Nuitka
           builds for documentation-only commits to `main`.
        """
        if self.head_branch and self.head_branch in SKIP_BINARY_BUILD_BRANCHES:
            logging.info(
                f"Branch {self.head_branch!r} is in SKIP_BINARY_BUILD_BRANCHES. "
                "Binary build will be skipped."
            )
            return True

        if (
            self.event_type == WorkflowEvent.push
            and self.head_commit_message
            and any(
                self.head_commit_message.startswith(prefix)
                for prefix in MANUAL_VERSION_BUMP_COMMIT_PREFIXES
            )
        ):
            logging.info(
                "Head commit is a user-initiated version bump "
                f"({self.head_commit_message.splitlines()[0]!r}). "
                "Binary build will be skipped."
            )
            return True

        # For push events, check if changed files affect binaries.
        if self.event_type == WorkflowEvent.push and self.changed_files is not None:
            affecting = self.binary_affecting_paths
            if not self.changed_files:
                # No changed files means nothing to build.
                logging.info("No changed files detected. Binary build will be skipped.")
                return True
            if not any(
                f.startswith(prefix) for f in self.changed_files for prefix in affecting
            ):
                logging.info(
                    f"No changed files match binary-affecting paths {affecting!r}. "
                    "Binary build will be skipped."
                )
                return True

        return False

    @cached_property
    def commit_range(self) -> tuple[str | None, str] | None:
        """Range of commits bundled within the triggering event.

        A workflow run is triggered by a singular event, which might encapsulate one or
        more commits. This means the workflow will only run once on the last commit,
        even if multiple new commits were pushed.

        This is critical for releases where two commits are pushed together:

        1. `[changelog] Release vX.Y.Z` — the release commit
        2. `[changelog] Post-release bump vX.Y.Z → vX.Y.Z` — the post-release bump

        Without extracting the full commit range, the release commit would be missed
        since `github.event.head_commit` only exposes the post-release bump.

        This property also enables processing each commit individually when we want to
        keep a carefully constructed commit history. The typical example is a pull
        request that is merged upstream but we'd like to produce artifacts (builds,
        packages, etc.) for each individual commit.

        The default `GITHUB_SHA` environment variable is not enough as it only points
        to the last commit. We need to inspect the commit history to find all new ones.
        New commits need to be fetched differently in `push` and `pull_request`
        events.

        ```{seealso}
        - https://stackoverflow.com/a/67204539
        - https://stackoverflow.com/a/62953566
        - https://stackoverflow.com/a/61861763
        ```

        ```{seealso}
        Pull request events on GitHub are a bit complex, see: [The Many SHAs of a GitHub Pull Request](https://www.kenmuse.com/blog/the-many-shas-of-a-github-pull-request/).
        ```
        """
        if not self.github_event or not self.event_type:
            return None
        # Pull request event.
        if self.event_type in (
            WorkflowEvent.pull_request,
            WorkflowEvent.pull_request_target,
        ):
            pr_data = self.github_event.get("pull_request", {})
            start = pr_data.get("base", {}).get("sha")
            # We need to checkout the HEAD commit instead of the artificial merge
            # commit introduced by the pull request.
            end = pr_data.get("head", {}).get("sha")
        # Push event.
        else:
            start = self.github_event.get("before")
            end = self.sha
        logging.debug(f"Commit range: {start} -> {end}")
        if not start or not end:
            logging.warning(f"Incomplete commit range: {start} -> {end}")
        return start, end

    @cached_property
    def current_commit(self) -> Commit:
        """Returns the current `Commit` object.

        Raises if `HEAD` cannot be resolved (an empty repository), mirroring the
        previous behavior where traversing an empty history raised too.
        """
        return get_commit("HEAD")

    @cached_property
    def current_commit_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of the current commit."""
        return self.commit_matrix((self.current_commit,))

    @cached_property
    def new_commits(self) -> tuple[Commit, ...] | None:
        """Returns list of all `Commit` objects bundled within the triggering event.

        This extracts **all commits** from the push event, not just `head_commit`.
        For releases, this typically includes both the release commit and the
        post-release bump commit, allowing downstream jobs to process each one.

        Commits are returned in chronological order (oldest first, most recent last).
        """
        if not self.commit_range:
            return None
        start, end = self.commit_range

        # Treat the null SHA as no start commit. GitHub sends this value when a tag is
        # created, since there is no previous commit to compare against.
        if start == NULL_SHA:
            logging.info(
                f"Start commit is null SHA ({NULL_SHA}), treating as no start commit."
            )
            start = None

        # Every branch below shells out to git, so mirror `changed_files`: a git
        # failure (a compiled binary run over a checkout git rejects as "dubious
        # ownership", a shallow clone that cannot be deepened) degrades to "range
        # unknown" instead of crashing the whole `metadata` command.
        try:
            # Sanity check: make sure both ends of the range exist in the repository.
            # Even though `start..end` excludes `start` from the result, git still
            # needs `start` present locally to resolve the range and walk history.
            for commit_id in (start, end):
                if not commit_id:
                    continue

                if not self.git_deepen(commit_id):
                    logging.warning(
                        "Skipping metadata extraction of the range of new commits."
                    )
                    return None

            if not start:
                logging.warning("No start commit found. Only one commit in range.")
                assert end
                return (get_commit(end),)

            # The `start..end` range already excludes `start`, so every returned
            # commit is a new one, in chronological order (oldest first).
            return list_commits(start, end)
        except subprocess.CalledProcessError as ex:
            detail = ex.stderr.strip() if ex.stderr else ex
            logging.warning(f"git failed while resolving new commits: {detail}")
            return None

    @cached_property
    def new_commits_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of new commits."""
        return self.commit_matrix(self.new_commits)

    @cached_property
    def new_commits_hash(self) -> tuple[str, ...] | None:
        """List all hashes of new commits."""
        return self.new_commits_matrix["commit"] if self.new_commits_matrix else None

    @cached_property
    def release_commits(self) -> tuple[Commit, ...] | None:
        """Returns list of `Commit` objects to be tagged within the triggering event.

        This filters `new_commits` to find release commits that need special handling:
        tagging, PyPI publishing, and GitHub release creation.

        This is essential because when a release is pushed, `github.event.head_commit`
        only exposes the post-release bump commit, not the release commit. By extracting
        all commits from the event (via `new_commits`) and filtering for release
        commits here, we ensure the release workflow can properly identify and process
        the `[changelog] Release vX.Y.Z` commit.

        We cannot identify a release commit based on the presence of a `vX.Y.Z` tag
        alone. That's because the tag is not present in the `prepare-release` pull
        request produced by the `changelog.yaml` workflow. The tag is created later
        by the `release.yaml` workflow, when the pull request is merged to `main`.

        Our best option is to identify a release based on the full commit message,
        using the template from the `changelog.yaml` workflow.
        """
        if not self.new_commits:
            return None
        return tuple(
            commit
            for commit in self.new_commits
            if RELEASE_COMMIT_PATTERN.fullmatch(commit.msg)
        )

    @cached_property
    def release_commits_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of release commits."""
        return self.commit_matrix(self.release_commits)

    @cached_property
    def release_commits_hash(self) -> tuple[str, ...] | None:
        """List all hashes of release commits."""
        return (
            self.release_commits_matrix["commit"]
            if self.release_commits_matrix
            else None
        )

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
    def is_python_project(self) -> bool:
        """Returns `True` if repository is a Python project.

        Presence of a `pyproject.toml` file that respects the standards is enough
        to consider the project as a Python one. Delegates to
        {func}`repomatic.pyproject.is_python_project` so the detection rule has
        a single source of truth.
        """
        return _is_python_project(pyproject_data=self.pyproject_toml)

    @cached_property
    def is_python_package(self) -> bool:
        """Returns `True` if the repository builds a distributable package.

        Strictly narrower than {attr}`is_python_project`: a uv virtual project
        declares a `[project]` table to carry its dependencies, then opts out of
        being built with `[tool.uv] package = false`. Delegates to
        {func}`repomatic.pyproject.is_python_package`, the same predicate
        {attr}`~repomatic.registry.RepoScope.PACKAGE_ONLY` resolves against, so
        the release lane and the checks that police it agree on who publishes.

        Prefer this over the truthiness of {attr}`package_name` when gating
        anything about publishing. `package_name` only reports what `[project]
        name` says, which a virtual project still declares.
        """
        return _is_python_package(pyproject_data=self.pyproject_toml)

    @cached_property
    def pyproject_toml(self) -> dict[str, Any]:
        """Returns the raw parsed content of `pyproject.toml`.

        Returns an empty dict if the file does not exist.
        """
        if self.pyproject_path.exists() and self.pyproject_path.is_file():
            data: dict[str, Any] = tomlrt.loads(
                self.pyproject_path.read_text(encoding="UTF-8")
            )
            return data
        return {}

    @cached_property
    def pyproject(self) -> StandardMetadata | None:
        """Returns metadata stored in the `pyproject.toml` file.

        Returns `None` if the `pyproject.toml` does not exists or does not respects
        the PEP standards.

        ```{warning}
        Some third-party apps have their configuration saved into
        `pyproject.toml` file, but that does not means the project is a Python
        one. For that, the `pyproject.toml` needs to respect the PEPs.
        ```
        """
        toml = self.pyproject_toml
        if toml:
            try:
                return StandardMetadata.from_pyproject(toml)
            except ConfigurationError:
                pass
        return None

    @cached_property
    def config(self) -> Config:
        """Returns the `[tool.repomatic]` section from `pyproject.toml`.

        Merges user configuration with defaults from `Config`.
        """
        return load_repomatic_config(self.pyproject_toml)

    @cached_property
    def nuitka_entry_points(self) -> list[str]:
        """Entry points selected for Nuitka binary compilation.

        Reads `[tool.repomatic].nuitka.entry-points` from `pyproject.toml`.
        When empty (the default), deduplicates by callable target: keeps the
        first entry point for each unique `module:callable` pair, so alias
        entry points (like both `mpm` and `meta-package-manager` pointing to
        the same function) don't produce duplicate binaries.
        Unrecognized CLI IDs are logged as warnings and discarded.
        """
        all_cli_ids = [cli_id for cli_id, _, _ in self.script_entries]
        if not all_cli_ids:
            return []

        raw = self.config.nuitka_entry_points
        if not raw:
            # Default: first entry point per unique callable target.
            seen_targets: set[str] = set()
            unique: list[str] = []
            for cli_id, module_id, callable_id in self.script_entries:
                target = f"{module_id}:{callable_id}"
                if target not in seen_targets:
                    seen_targets.add(target)
                    unique.append(cli_id)
            return unique

        selected = []
        for cli_id in raw:
            if cli_id in all_cli_ids:
                selected.append(cli_id)
            else:
                logging.warning(
                    f"Unrecognized nuitka entry point {cli_id!r}; valid: {all_cli_ids}"
                )
        return selected or all_cli_ids[:1]

    @cached_property
    def dev_targets(self) -> set[str]:
        """Nuitka build targets compiled on ordinary (non-release) pushes.

        Reads `[tool.repomatic].nuitka.dev-targets` from `pyproject.toml`. An
        empty list disables dev builds entirely. See
        {attr}`~repomatic.config.Config.nuitka_dev_targets` for the default and
        the canary rationale.

        Unrecognized target names are logged as warnings and discarded.
        """
        return _known_build_targets(self.config.nuitka_dev_targets, "dev")

    @cached_property
    def unstable_targets(self) -> set[str]:
        """Nuitka build targets allowed to fail without blocking the release.

        Reads `[tool.repomatic].nuitka.unstable-targets` from `pyproject.toml`.
        Defaults to an empty set.

        Unrecognized target names are logged as warnings and discarded.
        """
        return _known_build_targets(self.config.nuitka_unstable_targets, "unstable")

    @cached_property
    def package_name(self) -> str | None:
        """Returns package name as published on PyPI."""
        if self.pyproject and self.pyproject.canonical_name:
            return self.pyproject.canonical_name
        return None

    @cached_property
    def project_description(self) -> str | None:
        """Returns project description from pyproject.toml."""
        if self.pyproject and self.pyproject.description:
            return self.pyproject.description
        return None

    @cached_property
    def script_entries(self) -> list[tuple[str, str, str]]:
        """Returns a list of tuples containing the script name, its module and
        callable.

        Results are derived from the script entries of `pyproject.toml`. So that:

        ```{code-block} toml
        [project.scripts]
        mdedup = "mail_deduplicate.cli:mdedup"
        mpm = "meta_package_manager.__main__:main"
        ```

        Will yields the following list:

        ```{code-block} python
        (
            ("mdedup", "mail_deduplicate.cli", "mdedup"),
            ("mpm", "meta_package_manager.__main__", "main"),
            ...,
        )
        ```

        Each entry is validated against PEP 621 and PyPI conventions:

        - The script *name* (the dict key) must be non-empty, contain at
          least one non-dot character, and match `[A-Za-z0-9._-]+`. This
          mirrors the rule PyPI enforces on uploaded wheels and the check
          [uv-build performs](https://github.com/astral-sh/uv/pull/19495);
          rejecting names like `../escape`, `nested/script` or `.` here keeps
          them from flowing into the binary file path template
          `{{cli_id}}-{{current_version}}-{{target}}.{{extension}}` and from
          there into shell-quoted artifact names, `chmod`, and attestation
          commands in the release workflow.
        - The script *value* must split on `:` into exactly two non-empty
          parts (`module:object`). Malformed values raise a descriptive
          `ValueError` instead of crashing with an unpacking error.
        """
        entries = []
        if self.pyproject:
            for cli_id, script in self.pyproject.scripts.items():
                if not _SCRIPT_NAME_RE.fullmatch(cli_id) or all(
                    c == "." for c in cli_id
                ):
                    raise ValueError(
                        f"Invalid [project.scripts] name {cli_id!r}: must"
                        " contain at least one non-dot character and match"
                        " [A-Za-z0-9._-]+."
                    )
                parts = script.split(":")
                if len(parts) != 2 or not all(parts):
                    raise ValueError(
                        f"Invalid [project.scripts] value {script!r} for"
                        f" {cli_id!r}: expected the form 'module:object'."
                    )
                module_id, callable_id = parts
                entries.append((cli_id, module_id, callable_id))
        # Double check we do not have duplicate entries.
        all_cli_ids = [cli_id for cli_id, _, _ in entries]
        assert len(set(all_cli_ids)) == len(all_cli_ids)
        return entries

    @cached_property
    def requires_python_floor(self) -> tuple[int, int] | None:
        """The project's `requires-python` lower bound, as `(major, minor)`.

        The one reduction of the specifier to a floor, shared by
        {attr}`mypy_params` and the `lint-repo` Python-consistency check so the
        two cannot disagree on which operators count as a bound.

        :return: The first `>=`/`>` bound's release pair, or `None` when the
            project declares no `requires-python` or no lower bound.
        """
        if not self.pyproject or not self.pyproject.requires_python:
            return None
        for spec in self.pyproject.requires_python:
            if spec.operator in (">=", ">"):
                release = Version(spec.version).release
                return (release[0], release[1])
        return None

    @cached_property
    def mypy_params(self) -> list[str] | None:
        """Generates `mypy` parameters.

        Mypy needs to be fed with this parameter: `--python-version 3.x`.

        Extracts the minimum Python version from the project's `requires-python`
        specifier. Only takes `major.minor` into account.
        """
        min_version = self.requires_python_floor
        if not min_version:
            return None

        # Compare to Mypy's lowest supported version of Python dialect.
        major, minor = max(MYPY_VERSION_MIN, min_version)
        return ["--python-version", f"{major}.{minor}"]

    @staticmethod
    def get_current_version() -> str | None:
        """Returns the current version as managed by bump-my-version.

        Same as calling the CLI:

            ```{code-block} shell-session
            $ bump-my-version show current_version
            ```

        Reads `current_version` from the first TOML file found in the
        current working directory: `.bumpversion.toml` (top-level table) or
        `pyproject.toml` (`[tool.bumpversion]`).
        """
        cwd = Path.cwd()
        for filename, section_path in (
            (".bumpversion.toml", ()),
            ("pyproject.toml", ("tool", "bumpversion")),
        ):
            path = cwd / filename
            if not path.exists():
                continue
            try:
                data = tomlrt.loads(path.read_text(encoding="UTF-8")).to_dict()
            except tomlrt.TOMLParseError:
                continue
            section = data
            for key in section_path:
                section = section.get(key, {})
                if not isinstance(section, dict):
                    section = {}
                    break
            version = section.get("current_version")
            if version is not None:
                return str(version)
        return None

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
        """
        version = None
        if self.new_commits_matrix:
            details = self.new_commits_matrix.include
            if details:
                version = details[0].get("current_version")
        else:
            version = self.get_current_version()
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
    def is_sphinx(self) -> bool:
        """Returns `True` if the Sphinx config file is present."""
        # The Sphinx config file is present, that's enough for us.
        return self.sphinx_conf_path.exists() and self.sphinx_conf_path.is_file()

    @cached_property
    def minor_bump_allowed(self) -> bool:
        """Check if a minor version bump is allowed.

        This prevents double version increments within a development cycle.
        """
        return is_version_bump_allowed("minor")

    @cached_property
    def major_bump_allowed(self) -> bool:
        """Check if a major version bump is allowed.

        This prevents double version increments within a development cycle.
        """
        return is_version_bump_allowed("major")

    def _has_sphinx_extension(self, extension_name: str) -> bool:
        """Check if a Sphinx extension is listed in `conf.py`'s `extensions`.

        Parses the Sphinx configuration file as an AST and looks for an
        `extensions = [...]` assignment containing `extension_name`.
        """
        if not self.is_sphinx:
            return False
        for node in ast.parse(self.sphinx_conf_path.read_bytes()).body:
            if isinstance(node, ast.Assign) and isinstance(
                node.value, ast.List | ast.Tuple
            ):
                extension_found = "extensions" in (
                    t.id  # type: ignore[attr-defined]
                    for t in node.targets
                )
                if extension_found:
                    elements = (
                        e.value for e in node.value.elts if isinstance(e, ast.Constant)
                    )
                    if extension_name in elements:
                        return True
        return False

    @cached_property
    def active_autodoc(self) -> bool:
        """Returns `True` if Sphinx autodoc is active."""
        return self._has_sphinx_extension("sphinx.ext.autodoc")

    @cached_property
    def uses_myst(self) -> bool:
        """Returns `True` if MyST-Parser is active in Sphinx."""
        return self._has_sphinx_extension("myst_parser")

    @cached_property
    def nuitka_matrix(self) -> Matrix | None:
        """Pre-compute a matrix for Nuitka compilation workflows.

        Crosses three axes:

        - one commit per release commit (during a release) or per new commit
          (otherwise)
        - every `[project.scripts]` entry point
        - every build target of {data}`~repomatic.binary.NUITKA_BUILD_TARGETS`
          (runner, platform, architecture, binary extension, and the glibc floor
          or minimum-OS version that target enforces), narrowed to the
          `[tool.repomatic] nuitka.dev-targets` canary subset on an ordinary
          push (see {attr}`dev_targets`); release commits, `schedule` and
          `workflow_dispatch` runs keep the full roster

        Each axis contributes an `include` entry carrying the extra parameters
        the compile job needs, keyed on the axis value that selects it: the
        target's runner and floors, the entry point's module and callable, and
        the commit's short SHA and version. A final pass adds one `include` entry
        per `(os, entry_point, commit)` triple naming the `bin_name` the compiled
        artifact takes, since that name depends on all three at once.

        The matrix closes with `{"state": "stable"}`, which the release workflow
        reads to decide whether a failing job blocks the release.

        ```{note}
        Every value comes from {data}`~repomatic.binary.NUITKA_BUILD_TARGETS`
        and the project's own `pyproject.toml`, so no literal is repeated here:
        run `repomatic metadata nuitka_matrix` against a project to see the
        matrix it computes, or `repomatic show-test-matrix` for the test one.
        ```

        ```{todo}
        Drop the per-entry-point `--python-flag=-m` workaround computed below,
        and compile a `__main__.py` entry point through Nuitka's own
        `--main-entry-point`, once
        [Nuitka#3879](https://github.com/Nuitka/Nuitka/issues/3879) ships.
        ```
        """
        # Only produce a matrix if the project is providing CLI entry points.
        if not self.script_entries:
            return None

        # Allow projects to opt out of Nuitka compilation via pyproject.toml.
        if not self.config.nuitka_enabled:
            logging.info(
                "[tool.repomatic] nuitka.enabled is disabled."
                " Skipping binary compilation."
            )
            return None

        # On an ordinary push, compile only the canary subset: the full fleet
        # exists to refresh the rolling dev pre-release (a draft), and its
        # compile jobs contend for the account-wide runner cap on every code
        # push. Release commits, the weekly `schedule` trigger,
        # `workflow_dispatch` and local runs keep the full roster.
        build_targets = NUITKA_BUILD_TARGETS
        if self.event_type is WorkflowEvent.push and not self.release_commits_matrix:
            build_targets = {
                target_id: target_data
                for target_id, target_data in NUITKA_BUILD_TARGETS.items()
                if target_id in self.dev_targets
            }
            if not build_targets:
                logging.info(
                    "[tool.repomatic] nuitka.dev-targets selects no target."
                    " Skipping binary compilation for this push."
                )
                return None

        matrix = Matrix()

        # Register all runners on which we want to run Nuitka builds.
        matrix.add_variation(
            "os", tuple(target.runner for target in build_targets.values())
        )
        # Augment each "os" entry with platform-specific data.
        for build_target in build_targets.values():
            matrix.add_includes(build_target.as_matrix_entry())

        # `[tool.nuitka]` is not assembled here: `repomatic run nuitka` resolves
        # it at build time (the tool runner translates the section to CLI flags).
        # Only the per-entry-point --python-flag=-m workaround is computed below.

        # Filter entry points to those selected for Nuitka compilation.
        selected = set(self.nuitka_entry_points)
        for cli_id, module_id, callable_id in self.script_entries:
            if cli_id not in selected:
                continue
            # Derive CLI module path from its ID. Nuitka 4.1's
            # `--main-entry-point` flag is unusable on its own: it skips
            # populating `_main_paths` in `nuitka.importing.Importing` and
            # crashes with `NuitkaCodeDeficit: Error, cannot locate modules
            # before import mechanism is setup` inside
            # `setStandardLibraryModules`. Falling back to a positional module
            # path keeps `_main_paths` initialized via
            # `addMainScriptDirectory`.
            module_path = Path(f"{module_id.replace('.', '/')}.py")
            # That positional path is resolved from the repository root, which
            # a src-layout project does not expose: `mypkg.__main__` lives at
            # `src/mypkg/__main__.py`, not `mypkg/__main__.py`. Skip the entry
            # point instead of failing, so a project laid out that way still
            # gets its metadata (only its binaries are unavailable).
            if not module_path.exists():
                logging.warning(
                    f"Skipping Nuitka entry point {cli_id!r}: no module file "
                    f"at {module_path}."
                )
                continue
            # CLI ID is supposed to be unique, we'll use that as a key.
            matrix.add_variation("entry_point", [cli_id])

            # When the entry point is a `__main__.py` inside a package,
            # Nuitka expects the package directory (not the file) along
            # with `--python-flag=-m`.  Passing the file directly
            # produces a binary that silently exits without output.
            python_flags = ""
            if module_path.name == "__main__.py":
                package_dir = module_path.parent
                init_file = package_dir / "__init__.py"
                if init_file.exists():
                    module_path = package_dir
                    python_flags = "--python-flag=-m"

            matrix.add_includes({
                "entry_point": cli_id,
                "cli_id": cli_id,
                "module_id": module_id,
                "callable_id": callable_id,
                "module_path": str(module_path),
                "nuitka_python_flags": python_flags,
            })

        # Every selected entry point was skipped above. The `bin_name` template
        # below interpolates `cli_id`, so carrying on would raise instead of
        # reporting "nothing to compile".
        if "entry_point" not in matrix.variations:
            logging.warning(
                "No Nuitka entry point resolves to a module file."
                " Skipping binary compilation."
            )
            return None

        # For releases, only build binaries for the release (freeze) commits. The
        # post-release bump commit doesn't need binaries — only the freeze commit
        # gets tagged and attached to the GitHub release. This halves the number of
        # expensive Nuitka builds during the release cycle (6 instead of 12).
        # For non-release pushes, only build for the HEAD commit. Binary
        # compilation is expensive (6 OS/arch combinations × Nuitka), and the
        # workflow concurrency rule already cancels older runs for non-release
        # pushes — building every commit in a multi-commit push is wasteful.
        # Package builds (build-package job) still use new_commits_matrix
        # since they're cheap.
        build_commit_matrix = self.release_commits_matrix or self.current_commit_matrix
        assert build_commit_matrix
        # Extend the matrix with a new dimension: a list of commits.
        matrix.add_variation("commit", build_commit_matrix["commit"])
        matrix.add_includes(*build_commit_matrix.include)

        # Augment each variation set of the matrix with the binary name Nuitka
        # produces. Iterate over all matrix variation sets so we have all the
        # metadata needed to generate a name unique to these variations.
        for variations in matrix.solve():
            # We re-attach the binary name with an include directive, so we need a
            # copy of the main variants it corresponds to.
            bin_name_include = {k: variations[k] for k in matrix.variations}
            bin_name_include["bin_name"] = binary_name(
                variations["cli_id"],
                variations["target"],
                variations["current_version"],
            )
            matrix.add_includes(bin_name_include)

        # All jobs are stable by default, unless marked otherwise by specific
        # configuration. Unstable targets outside the selected subset are
        # dropped: an include whose "os" matches no matrix combination would
        # be added by GitHub as a new, half-formed combination.
        matrix.add_includes({"state": "stable"})
        for unstable_target in self.unstable_targets:
            if unstable_target not in build_targets:
                continue
            matrix.add_includes({
                "state": "unstable",
                "os": NUITKA_BUILD_TARGETS[unstable_target].runner,
            })

        return matrix

    def _apply_test_matrix_config(self, matrix: Matrix, full: bool = False) -> None:
        """Apply per-project `[tool.repomatic.test-matrix]` config to a matrix.

        :param matrix: The matrix to modify in-place.
        :param full: If `True`, also apply `variations` (extra dimension
            values) and `unstable` (continue-on-error markings). Both are added
            to the full matrix only, not the PR matrix, to keep PR CI fast and
            stable.
        """
        # Replacements first, then removals: both modify axis values in-place.
        for var_id, mapping in self.config.test_matrix.replace.items():
            for old, new in mapping.items():
                matrix.replace_variation_value(var_id, old, new)
        for var_id, values in self.config.test_matrix.remove.items():
            for value in values:
                matrix.remove_variation_value(var_id, value)
        if full:
            for var_id, values in self.config.test_matrix.variations.items():
                matrix.add_variation(var_id, values)
            # Mark matching combinations continue-on-error via a `state`
            # include. Full matrix only: in the PR matrix a non-base axis key
            # (e.g. click-version) would be added to every job and hijack it.
            for combination in self.config.test_matrix.unstable:
                matrix.add_includes({**combination, "state": "unstable"})
        if self.config.test_matrix.exclude:
            matrix.add_excludes(*self.config.test_matrix.exclude)
        if self.config.test_matrix.include:
            matrix.add_includes(*self.config.test_matrix.include)
        # Drop excludes that became no-ops after replace/remove changed the
        # axes, so GitHub Actions does not reject the matrix. No-op user
        # excludes that are likely typos are surfaced separately by the
        # lint-repo check (see Metadata.stale_test_matrix_excludes).
        matrix.prune()

    @cached_property
    def _test_matrix_base(self) -> Matrix:
        """Full test matrix in axes form, before any `full-include` flattening.

        The cross-product of OS images and Python versions plus per-project
        variations, with includes and excludes applied. {attr}`test_matrix`
        flattens this to an explicit job list when `full-include` rows are
        configured; the stale-exclude lint check reads its axis values from
        here, so it keeps working whichever form {attr}`test_matrix` emits.
        """
        matrix = Matrix()
        matrix.add_variation("os", TEST_RUNNERS_FULL)
        matrix.add_variation("python-version", TEST_PYTHON_FULL)
        removed_os = self.config.test_matrix.remove.get("os", ())
        # Python 3.10 has no native ARM64 Windows build. Skip this guard when
        # the project removes windows-11-arm, so it does not linger as a no-op
        # exclude that prune would warn about.
        if "windows-11-arm" not in removed_os:
            matrix.add_excludes({"os": "windows-11-arm", "python-version": "3.10"})
        matrix.add_includes({"state": "stable"})
        # `python-label` is display-only: it spells the version the way pyenv and
        # actions/setup-python name a prerelease, so the job title says why the
        # cell is continue-on-error. It rides alongside the version rather than
        # replacing it because uv rejects the `-dev` form, and because downstream
        # `test-matrix` directives key on the bare version. See
        # {data}`~repomatic.matrix_axes.PRERELEASE_LABEL_SUFFIX`.
        for version in sorted(UNSTABLE_PYTHON_VERSIONS):
            matrix.add_includes({
                "state": "unstable",
                "python-version": version,
                "python-label": f"{version}{PRERELEASE_LABEL_SUFFIX}",
            })
        # Released build flavors (free-threaded) are a variant of an
        # already-broadly-covered base version, so they smoke-test stable on a
        # single runner rather than the full spread. Each is a standalone
        # include pinned to its runner (it introduces a python-version absent
        # from the axis, so it joins one runner instead of multiplying across
        # the os axis); skip it when that runner was removed.
        for version, keep_os in sorted(SINGLE_RUNNER_PYTHON_VERSIONS.items()):
            if keep_os not in removed_os:
                matrix.add_includes({
                    "os": keep_os,
                    "python-version": version,
                    "state": "stable",
                })
        self._apply_test_matrix_config(matrix, full=True)
        return matrix

    @cached_property
    def test_matrix(self) -> Matrix:
        """Full test matrix for non-PR events.

        Combines all runner OS images and Python versions, excluding known
        incompatible combinations. Marks development Python versions as
        unstable so CI can use `continue-on-error`, and adds released build
        flavors (free-threaded) as stable single-runner smoke tests. Per-project
        config from `[tool.repomatic.test-matrix]` is applied last.

        When `[tool.repomatic.test-matrix] full-include` rows are configured,
        the matrix is emitted as a flat job list (`{"include": [...]}`) so each
        row is a standalone combination GitHub runs verbatim, rather than one
        that augments a base combo sharing its `os` and `python-version`.
        """
        base = self._test_matrix_base
        full_include = self.config.test_matrix.full_include
        if not full_include:
            return base
        # Single-key default includes (like {click-version: released}) that the
        # base matrix grants every cross-product job. Collect them first so they
        # backfill both the solved base jobs and the full-include rows below.
        defaults = {
            key: value
            for directive in base.include
            if len(directive) == 1
            for key, value in directive.items()
        }
        defaults.setdefault("state", "stable")
        # Solve the base cross-product to explicit jobs, then append each
        # full-include row as its own standalone job. Emitting this flat list
        # sidesteps GitHub's include augment-or-add ambiguity for rows sharing
        # an os/python with the shipped-config jobs. base.solve() also appends
        # free-threaded probe jobs from standalone includes, which GitHub never
        # augments with the defaults; backfill every cell so none emits an empty
        # click-version/cloup-version that GitHub expands to "".
        rows = [{**defaults, **cell} for cell in base.solve()]
        rows.extend({**defaults, **cell} for cell in full_include)
        flat = Matrix()
        flat.add_includes(*rows)
        return flat

    @cached_property
    def test_matrix_pr(self) -> Matrix:
        """Reduced test matrix for pull requests.

        Skips experimental Python versions and redundant architecture
        variants to reduce CI load on PRs. Per-project config excludes and
        includes from `[tool.repomatic.test-matrix]` are applied, but
        variations are not (to keep the PR matrix small).
        """
        matrix = Matrix()
        matrix.add_variation("os", TEST_RUNNERS_PR)
        matrix.add_variation("python-version", TEST_PYTHON_PR)
        matrix.add_includes({"state": "stable"})
        self._apply_test_matrix_config(matrix, full=False)
        return matrix

    @cached_property
    def stale_test_matrix_excludes(
        self,
    ) -> list[tuple[dict[str, str], dict[str, str]]]:
        """User `test-matrix.exclude` entries matching no full-matrix axis value.

        An exclude naming a value absent from every axis (like a renamed
        runner) can never match a combination, so `Matrix.prune()` drops it
        silently and its exclusion intent is lost. This drift is common after
        an upstream runner rename (such as `macos-15-intel` becoming
        `macos-26-intel`). The `lint-repo` check surfaces these so the drift
        fails loudly instead of silently.

        The axes come from {attr}`_test_matrix_base`, never from the emitted
        {attr}`test_matrix`: a `full-include` matrix emits as a flat job list
        whose `all_variations()` is empty, which would misreport every key of
        an entry as stale. Carrying the absent values in the result is what
        keeps the lint check from re-deriving them against the wrong matrix.

        :return: `(entry, absent_values)` pairs in config order: each
            offending exclude with the key/value pairs no axis carries.
        """
        axes = self._test_matrix_base.all_variations()
        return [
            (entry, bad)
            for entry in self.config.test_matrix.exclude
            if (bad := stale_axis_values(entry, axes))
        ]

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
