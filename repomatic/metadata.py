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

The following variables are [printed to the environment file](https://docs.github.com/en/free-pro-team@latest/actions/reference/workflow-commands-for-github-actions#environment-files):

```{code-block} text
is_bot=false
new_commits=346ce664f055fbd042a25ee0b7e96702e95 6f27db47612aaee06fdf08744b09a9f5f6c2
release_commits=6f27db47612aaee06fdf08744b09a9f5f6c2
mailmap_exists=true
gitignore_exists=true
python_files=".github/update_mailmap.py" ".github/metadata.py" "setup.py"
json_files=
yaml_files="config.yaml" ".github/workflows/lint.yaml" ".github/workflows/test.yaml"
workflow_files=".github/workflows/lint.yaml" ".github/workflows/test.yaml"
doc_files="changelog.md" "readme.md" "docs/license.md"
markdown_files="changelog.md" "readme.md" "docs/license.md"
image_files=
zsh_files=
is_python_project=true
package_name=click-extra
project_description=📦 Extra colorful clickable helpers for the CLI.
mypy_params=--python-version 3.7
current_version=2.0.1
released_version=2.0.0
is_sphinx=true
active_autodoc=true
release_notes=[🐍 Available on PyPI](https://pypi.org/project/click-extra/2.21.3).
new_commits_matrix={
    "commit": [
        "346ce664f055fbd042a25ee0b7e96702e95",
        "6f27db47612aaee06fdf08744b09a9f5f6c2"
    ],
    "include": [
        {
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "short_sha": "346ce66",
            "current_version": "2.0.1"
        },
        {
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "short_sha": "6f27db4",
            "current_version": "2.0.0"
        }
    ]
}
release_commits_matrix={
    "commit": ["6f27db47612aaee06fdf08744b09a9f5f6c2"],
    "include": [
        {
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "short_sha": "6f27db4",
            "current_version": "2.0.0"
        }
    ]
}
build_targets=[
    {
        "target": "linux-arm64",
        "os": "ubuntu-24.04-arm",
        "platform_id": "linux",
        "arch": "arm64",
        "extension": "bin"
    },
    {
        "target": "linux-x64",
        "os": "ubuntu-24.04",
        "platform_id": "linux",
        "arch": "x64",
        "extension": "bin"
    },
    {
        "target": "macos-arm64",
        "os": "macos-26",
        "platform_id": "macos",
        "arch": "arm64",
        "extension": "bin"
    },
    {
        "target": "macos-x64",
        "os": "macos-26-intel",
        "platform_id": "macos",
        "arch": "x64",
        "extension": "bin"
    },
    {
        "target": "windows-arm64",
        "os": "windows-11-arm",
        "platform_id": "windows",
        "arch": "arm64",
        "extension": "exe"
    },
    {
        "target": "windows-x64",
        "os": "windows-2025",
        "platform_id": "windows",
        "arch": "x64",
        "extension": "exe"
    }
]
nuitka_matrix={
    "os": [
        "ubuntu-24.04-arm",
        "ubuntu-24.04",
        "macos-26",
        "macos-26-intel",
        "windows-11-arm",
        "windows-2025"
    ],
    "entry_point": ["mpm"],
    "commit": [
        "346ce664f055fbd042a25ee0b7e96702e95",
        "6f27db47612aaee06fdf08744b09a9f5f6c2"
    ],
    "include": [
        {
            "target": "linux-arm64",
            "os": "ubuntu-24.04-arm",
            "platform_id": "linux",
            "arch": "arm64",
            "extension": "bin"
        },
        {
            "target": "linux-x64",
            "os": "ubuntu-24.04",
            "platform_id": "linux",
            "arch": "x64",
            "extension": "bin"
        },
        {
            "target": "macos-arm64",
            "os": "macos-26",
            "platform_id": "macos",
            "arch": "arm64",
            "extension": "bin"
        },
        {
            "target": "macos-x64",
            "os": "macos-26-intel",
            "platform_id": "macos",
            "arch": "x64",
            "extension": "bin"
        },
        {
            "target": "windows-arm64",
            "os": "windows-11-arm",
            "platform_id": "windows",
            "arch": "arm64",
            "extension": "exe"
        },
        {
            "target": "windows-x64",
            "os": "windows-2025",
            "platform_id": "windows",
            "arch": "x64",
            "extension": "exe"
        },
        {
            "entry_point": "mpm",
            "cli_id": "mpm",
            "module_id": "meta_package_manager.__main__",
            "callable_id": "main",
            "module_path": "meta_package_manager"
        },
        {
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "short_sha": "346ce66",
            "current_version": "2.0.0"
        },
        {
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "short_sha": "6f27db4",
            "current_version": "1.9.1"
        },
        {
            "os": "ubuntu-24.04-arm",
            "entry_point": "mpm",
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "bin_name": "mpm-linux-arm64.bin"
        },
        {
            "os": "ubuntu-24.04-arm",
            "entry_point": "mpm",
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "bin_name": "mpm-linux-arm64.bin"
        },
        {
            "os": "ubuntu-24.04",
            "entry_point": "mpm",
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "bin_name": "mpm-linux-x64.bin"
        },
        {
            "os": "ubuntu-24.04",
            "entry_point": "mpm",
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "bin_name": "mpm-linux-x64.bin"
        },
        {
            "os": "macos-26",
            "entry_point": "mpm",
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "bin_name": "mpm-macos-arm64.bin"
        },
        {
            "os": "macos-26",
            "entry_point": "mpm",
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "bin_name": "mpm-macos-arm64.bin"
        },
        {
            "os": "macos-26-intel",
            "entry_point": "mpm",
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "bin_name": "mpm-macos-x64.bin"
        },
        {
            "os": "macos-26-intel",
            "entry_point": "mpm",
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "bin_name": "mpm-macos-x64.bin"
        },
        {
            "os": "windows-11-arm",
            "entry_point": "mpm",
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "bin_name": "mpm-windows-arm64.bin"
        },
        {
            "os": "windows-11-arm",
            "entry_point": "mpm",
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "bin_name": "mpm-windows-arm64.bin"
        },
        {
            "os": "windows-2025",
            "entry_point": "mpm",
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "bin_name": "mpm-windows-x64.exe"
        },
        {
            "os": "windows-2025",
            "entry_point": "mpm",
            "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
            "bin_name": "mpm-windows-x64.exe"
        },
        {"state": "stable"}
    ]
}
```

```{warning}
Fields with serialized lists and dictionaries, like `new_commits_matrix`,
`build_targets` or `nuitka_matrix`, are pretty-printed in the example above for
readability. They are inlined in the actual output and not formatted this way.
```
"""

from __future__ import annotations

import ast
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import fields
from functools import cached_property
from operator import itemgetter
from pathlib import Path

from bumpversion.config import get_configuration
from bumpversion.config.files import find_config_file
from bumpversion.show import resolve_name
from extra_platforms import is_github_ci
from git.exc import GitCommandError
from gitdb.exc import BadName
from packaging.version import Version
from py_walk import get_parser_from_file
from py_walk.models import Parser
from pydriller import Commit, Git, Repository
from pyproject_metadata import ConfigurationError, StandardMetadata
from typing_extensions import Self
from wcmatch.glob import (
    BRACE,
    DOTGLOB,
    FOLLOW,
    GLOBSTAR,
    GLOBTILDE,
    NEGATE,
    NODIR,
    iglob,
)

from .binary import (
    BINARY_AFFECTING_PATHS,
    FLAT_BUILD_TARGETS,
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    NUITKA_BUILD_TARGETS,
    SKIP_BINARY_BUILD_BRANCHES,
    nuitka_args_from_pyproject,
)
from .changelog import (
    GITHUB_RELEASE_URL,
    Changelog,
    build_release_admonition,
)
from .git_ops import (
    RELEASE_COMMIT_PATTERN,
    SHORT_SHA_LENGTH,
    get_latest_tag_version,
    get_release_version_from_commits,
    get_repo_slug_from_remote,
)
from .github.actions import NULL_SHA, WorkflowEvent, generate_delimiter
from .github.gh import run_gh_command
from .github.matrix import Matrix
from .mailmap import MAILMAP_PATH
from .pypi import PYPI_PROJECT_URL
from .test_matrix import (
    MYPY_VERSION_MIN,
    TEST_PYTHON_FULL,
    TEST_PYTHON_PR,
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
    UNSTABLE_PYTHON_VERSIONS,
)

if sys.version_info >= (3, 11):
    from enum import StrEnum

    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]
    from backports.strenum import StrEnum

from .config import (
    SUBCOMMAND_CONFIG_FIELDS,
    Config,
    _extract_field_docstrings,
    load_repomatic_config,
)
from .pyproject import is_python_project as _is_python_project

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final, Literal

    from typing_extensions import Self


class Dialect(StrEnum):
    """Output dialect for metadata serialization."""

    github = "github"
    github_json = "github-json"
    json = "json"


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
    "renovate_config_exists": "Whether a Renovate configuration file exists.",
    "python_files": "List of Python files in the repository.",
    "json_files": "List of JSON files in the repository.",
    "yaml_files": "List of YAML files in the repository.",
    "toml_files": "List of TOML files in the repository.",
    "pyproject_files": "List of pyproject.toml files in the repository.",
    "workflow_files": "List of GitHub workflow files.",
    "doc_files": "List of documentation files.",
    "markdown_files": "List of Markdown files.",
    "image_files": "List of image files.",
    "shfmt_files": "List of shell files formattable by shfmt.",
    "zsh_files": "List of Zsh files.",
    "is_python_project": "Repository is a Python project with pyproject.toml.",
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
}
"""One-liner descriptions for each metadata key produced by {meth}`Metadata.dump`."""


METADATA_KEYS_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Key", "key"),
    ("Description", "description"),
)
"""Column definitions for the metadata keys reference table."""


def metadata_keys_reference() -> list[tuple[str, str]]:
    """Build the metadata keys reference as table rows.

    Returns a list of `(key, description)` tuples for all keys produced by
    {meth}`Metadata.dump`, including `[tool.repomatic]` config fields that are
    exposed as metadata outputs. Rows are unsorted: sorting is handled by the
    CLI's `SortByOption`.
    """
    rows = [(k, v) for k, v in _METADATA_KEY_DESCRIPTIONS.items()]

    # Add config fields exposed as metadata (same filter as dump()).
    docstrings = _extract_field_docstrings()
    for f in fields(Config):
        if (
            f.name
            not in (
                "nuitka_entry_points",
                "nuitka_extra_args",
                "nuitka_unstable_targets",
            )
            and f.name not in SUBCOMMAND_CONFIG_FIELDS
        ):
            desc = docstrings.get(f.name, "").replace("``", "`")
            rows.append((f.name, desc))

    return rows


def all_metadata_keys() -> frozenset[str]:
    """Returns the set of all valid metadata key names."""
    config_keys = frozenset(
        f.name
        for f in fields(Config)
        if f.name
        not in (
            "nuitka_entry_points",
            "nuitka_extra_args",
            "nuitka_unstable_targets",
        )
        and f.name not in SUBCOMMAND_CONFIG_FIELDS
    )
    return frozenset(_METADATA_KEY_DESCRIPTIONS) | config_keys


GITIGNORE_PATH = Path(".gitignore")

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

    def __init__(self) -> None:
        """Initialize internal variables."""

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
        """
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if not event_path:
            if is_github_ci():
                logging.warning("GITHUB_EVENT_PATH not set in environment.")
            return {}
        event_file = Path(event_path)
        if not event_file.exists():
            raise FileNotFoundError(f"Event file not found: {event_path}")
        event = json.loads(event_file.read_text(encoding="utf-8"))
        logging.debug("--- GitHub event payload ---")
        logging.debug(json.dumps(event, indent=4))
        return event  # type:ignore[no-any-return]

    @cached_property
    def git(self) -> Git:
        """Return a PyDriller Git object."""
        return Git(".")

    def git_stash_count(self) -> int:
        """Returns the number of stashes."""
        count = int(
            self.git.repo.git.rev_list(
                "--walk-reflogs", "--ignore-missing", "--count", "refs/stash"
            )
        )
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
            try:
                _ = self.git.get_commit(commit_hash)
            except (ValueError, BadName) as ex:
                logging.debug(f"Commit {commit_hash} not found: {ex}")

                # Only compute depth if not cached yet.
                if current_depth is None:
                    current_depth = self.git.total_commits()

                if attempt == max_attempts - 1:
                    # We've exhausted all attempts.
                    logging.error(
                        f"Cannot find commit {commit_hash} in repository after "
                        f"{max_attempts} deepen attempts. "
                        f"Final depth is {current_depth} commits."
                    )
                    return False

                logging.info(
                    f"Commit {commit_hash} not found at depth {current_depth}."
                )
                logging.info(
                    f"Deepening by {deepen_increment} commits (attempt "
                    f"{attempt + 1}/{max_attempts})..."
                )

                try:
                    self.git.repo.git.fetch(f"--deepen={deepen_increment}")
                    # Update cached depth after successful fetch.
                    current_depth = self.git.total_commits()
                    logging.debug(
                        f"Repository deepened successfully. New depth: {current_depth}"
                    )
                except GitCommandError as ex:
                    logging.error(f"Failed to deepen repository: {ex}")
                    return False
            else:
                if attempt > 0:
                    logging.info(
                        f"Found commit {commit_hash} after {attempt} deepen "
                        "operation(s)."
                    )
                return True

        return False

    def commit_matrix(self, commits: Iterable[Commit] | None) -> Matrix | None:
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

        current_commit = self.git.repo.head.commit.hexsha

        # Check if we need to get back in time in the Git log and browse past commits.
        if len(commits) == 1:  # type: ignore[arg-type]
            # Is the current commit the one we're looking for?
            past_commit_lookup = bool(
                current_commit != commits[0].hash  # type: ignore[index]
            )
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
            if self.git.repo.head.is_detached:
                init_ref = current_commit
            else:
                init_ref = self.git.repo.active_branch.name
            logging.debug(f"Initial commit reference: {init_ref}")

            # Try to stash local changes and check if we'll need to unstash them later.
            counter_before = self.git_stash_count()
            logging.debug("Try to stash local changes before our series of checkouts.")
            self.git.repo.git.stash()
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
                self.git.checkout(commit.hash)

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
            logging.debug(f"Restore repository to {init_ref}.")
            self.git.checkout(init_ref)
            if need_unstash:
                logging.debug("Unstash local changes that were previously saved.")
                self.git.repo.git.stash("pop")

        return matrix

    @cached_property
    def event_type(self) -> WorkflowEvent | None:
        """Returns the type of event that triggered the workflow run.

        ```{caution}
        This property is based on a crude heuristics as it only looks at the value
        of the `GITHUB_BASE_REF` environment variable. Which is [only set when the event that triggers a workflow run is either pull_request or pull_request_target](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#default-environment-variables).
        ```

        ```{todo}
        Add detection of all workflow trigger events.
        ```
        """
        if not is_github_ci():
            logging.warning(
                "Cannot guess event type because we're not in a CI environment."
            )
            return None
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
        return os.environ.get("GITHUB_ACTOR")

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

        Also detects Renovate PRs by branch name pattern (`renovate/*`), which handles
        cases where Renovate runs as a user account rather than the `renovate[bot]` app.
        """
        # XXX replace by self.event_sender_type != "User"?
        if self.event_sender_type == "Bot" or self.event_actor in (
            "dependabot[bot]",
            "dependabot-preview[bot]",
            "renovate[bot]",
        ):
            return True
        # Detect Renovate PRs by branch name pattern. This handles self-hosted Renovate
        # or cases where Renovate runs as a user account.
        return bool(self.head_branch and self.head_branch.startswith("renovate/"))

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

        Reads `GITHUB_EVENT_NAME`. This is the raw event name (e.g.,
        `"push"`, `"pull_request"`, `"workflow_run"`), as opposed to
        {attr}`event_type` which returns a {class}`WorkflowEvent` enum based
        on heuristics.
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
        return bool(name and name.startswith("awesome-"))

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
            diff_output = self.git.repo.git.diff("--name-only", start, end)
        except GitCommandError:
            logging.warning("Failed to get changed files from git diff.")
            return None
        if not diff_output:
            return ()
        return tuple(diff_output.strip().splitlines())

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
    def current_commit(self) -> Commit | None:
        """Returns the current `Commit` object."""
        return next(Repository(".", single="HEAD").traverse_commits())

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

        # Sanity check: make sure the start commit exists in the repository.
        # XXX Even if we skip the start commit later on (range is inclusive),
        # we still need to make sure it exists: PyDriller stills needs to
        # find it to be able to traverse the commit history.
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
            return (self.git.get_commit(end),)

        commit_list = []
        for index, commit in enumerate(
            Repository(".", from_commit=start, to_commit=end).traverse_commits()
        ):
            # Skip the first commit because the commit range is inclusive.
            if index == 0:
                continue
            commit_list.append(commit)
        return tuple(commit_list)

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
    def gitignore_exists(self) -> bool:
        return GITIGNORE_PATH.is_file()

    @cached_property
    def renovate_config_exists(self) -> bool:
        # Lazy import to avoid circular dependency:
        # metadata → renovate → github.pr_body → metadata.
        from .renovate import RENOVATE_CONFIG_PATH

        return RENOVATE_CONFIG_PATH.is_file()

    @cached_property
    def gitignore_parser(self) -> Parser | None:
        """Returns a parser for the `.gitignore` file, if it exists."""
        if self.gitignore_exists:
            logging.debug(f"Parse {GITIGNORE_PATH}")
            return get_parser_from_file(GITIGNORE_PATH)
        return None

    def gitignore_match(self, file_path: Path | str) -> bool:
        return bool(self.gitignore_parser and self.gitignore_parser.match(file_path))

    def glob_files(self, *patterns: str) -> list[Path]:
        """Return all file path matching the `patterns`.

        Patterns are glob patterns supporting `**` for recursive search, and `!`
        for negation.

        All directories are traversed, whether they are hidden (i.e. starting with a
        dot `.`) or not, including symlinks.

        Skips:

        - files which does not exists
        - directories
        - broken symlinks
        - files matching patterns specified by `.gitignore` file

        Returns both hidden and non-hidden files.

        All files are normalized to their absolute path, so that duplicates produced by
        symlinks are ignored.

        File path are returned as relative to the current working directory if
        possible, or as absolute path otherwise.

        The resulting list of file paths is sorted.
        """
        current_dir = Path.cwd()
        seen = set()

        for file_path in iglob(
            patterns,
            flags=NODIR | GLOBSTAR | DOTGLOB | GLOBTILDE | BRACE | FOLLOW | NEGATE,
        ):
            # Normalize the path to avoid duplicates.
            try:
                absolute_path = Path(file_path).resolve(strict=True)
            # Skip files that do not exists and broken symlinks.
            except OSError:
                logging.warning(f"Skip non-existing file / broken symlink: {file_path}")
                continue

            # Simplify the path by trying to make it relative to the current location.
            normalized_path = absolute_path
            try:
                normalized_path = absolute_path.relative_to(current_dir)
            except ValueError:
                # If the file is not relative to the current directory, keep its
                # absolute path.
                logging.debug(
                    f"{absolute_path} is not relative to {current_dir}. "
                    "Keeping the path absolute."
                )

            if normalized_path in seen:
                logging.debug(f"Skip duplicate file: {normalized_path}")
                continue

            # Skip files that are ignored by .gitignore.
            if self.gitignore_match(file_path):
                logging.debug(f"Skip file matching {GITIGNORE_PATH}: {file_path}")
                continue

            seen.add(normalized_path)
        return sorted(seen)

    @cached_property
    def python_files(self) -> list[Path]:
        """Returns a list of python files."""
        return self.glob_files("**/*.{py,pyi,pyw,pyx,ipynb}")

    @cached_property
    def json_files(self) -> list[Path]:
        """Returns a list of JSON files.

        ```{note}
        JSON5 files are excluded because Biome doesn't support them.
        ```
        """
        return self.glob_files(
            "**/*.{json,jsonc}",
            "**/.code-workspace",
            "!**/package-lock.json",
        )

    @cached_property
    def yaml_files(self) -> list[Path]:
        """Returns a list of YAML files."""
        return self.glob_files("**/*.{yaml,yml}")

    @cached_property
    def toml_files(self) -> list[Path]:
        """Returns a list of TOML files."""
        return self.glob_files("**/*.toml")

    @cached_property
    def pyproject_files(self) -> list[Path]:
        """Returns a list of `pyproject.toml` files."""
        return self.glob_files("**/pyproject.toml")

    @cached_property
    def workflow_files(self) -> list[Path]:
        """Returns a list of GitHub workflow files."""
        return self.glob_files(".github/workflows/**/*.{yaml,yml}")

    @cached_property
    def doc_files(self) -> list[Path]:
        """Returns a list of doc files."""
        return self.glob_files(
            "**/*.{markdown,mdown,mkdn,mdwn,mkd,md,mdtxt,mdtext,mdx,rst,tex}"
        )

    @cached_property
    def markdown_files(self) -> list[Path]:
        """Returns a list of Markdown files."""
        return self.glob_files(
            "**/*.{markdown,mdown,mkdn,mdwn,mkd,md,mdtxt,mdtext,mdx}"
        )

    @cached_property
    def image_files(self) -> list[Path]:
        """Returns a list of image files.

        Covers the formats handled by `repomatic format-images`: JPEG, PNG,
        WebP, and AVIF. See {mod}`repomatic.images` for the optimization tools.
        """
        return self.glob_files("**/*.{jpeg,jpg,png,webp,avif}")

    @cached_property
    def shfmt_files(self) -> list[Path]:
        """Returns a list of shell files that `shfmt` can reliably format.

        `shfmt` supports the following dialects (`-ln` flag):

        - **bash**: GNU Bourne Again Shell.
        - **posix**: POSIX Shell (`/bin/sh`).
        - **mksh**: MirBSD Korn Shell.
        - **bats**: Bash Automated Testing System.

        Zsh is excluded. `shfmt` added experimental Zsh support in v3.13.0
        but it fails on common constructs: `for var (list)` short-form loops
        and ``for ... { }`` brace-delimited loops. See [mvdan/sh#1203](https://github.com/mvdan/sh/issues/1203) for upstream tracking.

        Files are excluded by extension (`.zsh`, `.zshrc`, etc.) and by
        shebang (any `.sh` file whose first line references `zsh`).
        """
        candidates = self.glob_files(
            "**/*.{bash,bats,ksh,mksh,sh}",
            "**/.{bash_login,bash_logout,bash_profile,bashrc,profile}",
        )
        result = []
        for path in candidates:
            try:
                with path.open("rb") as fh:
                    first_line = fh.readline(256)
            except OSError:
                continue
            if first_line.startswith(b"#!") and b"zsh" in first_line:
                continue
            result.append(path)
        return result

    @cached_property
    def zsh_files(self) -> list[Path]:
        """Returns a list of Zsh files."""
        return self.glob_files("**/*.{sh,zsh}", "**/.{zshrc,zprofile,zshenv,zlogin}")

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
    def pyproject_toml(self) -> dict[str, Any]:
        """Returns the raw parsed content of `pyproject.toml`.

        Returns an empty dict if the file does not exist.
        """
        if self.pyproject_path.exists() and self.pyproject_path.is_file():
            data: dict[str, Any] = tomllib.loads(
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
    def unstable_targets(self) -> set[str]:
        """Nuitka build targets allowed to fail without blocking the release.

        Reads `[tool.repomatic].nuitka.unstable-targets` from `pyproject.toml`.
        Defaults to an empty set.

        Unrecognized target names are logged as warnings and discarded.
        """
        raw = self.config.nuitka_unstable_targets
        targets = set(raw)
        if targets:
            unknown = targets - set(NUITKA_BUILD_TARGETS)
            if unknown:
                logging.warning(f"Unrecognized unstable targets: {unknown}")
            targets &= set(NUITKA_BUILD_TARGETS)
        return targets

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
        """
        entries = []
        if self.pyproject:
            for cli_id, script in self.pyproject.scripts.items():
                module_id, callable_id = script.split(":")
                entries.append((cli_id, module_id, callable_id))
        # Double check we do not have duplicate entries.
        all_cli_ids = [cli_id for cli_id, _, _ in entries]
        assert len(set(all_cli_ids)) == len(all_cli_ids)
        return entries

    @cached_property
    def mypy_params(self) -> list[str] | None:
        """Generates `mypy` parameters.

        Mypy needs to be fed with this parameter: `--python-version 3.x`.

        Extracts the minimum Python version from the project's `requires-python`
        specifier. Only takes `major.minor` into account.
        """
        if not self.pyproject or not self.pyproject.requires_python:
            return None

        # Find the lower bound from the requires-python specifier.
        min_version = None
        for spec in self.pyproject.requires_python:
            if spec.operator in (">=", ">"):
                release = Version(spec.version).release
                min_version = (release[0], release[1])
                break

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
        """
        conf_file = find_config_file()
        if not conf_file:
            return None
        config = get_configuration(conf_file)
        config_dict = config.model_dump()
        return str(resolve_name(config_dict, "current_version"))

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

        Combine the variations of:
        - release commits only (during releases) or all new commits (otherwise)
        - all entry points
        - for the 3 main OSes
        - for a set of architectures

        Returns a ready-to-use matrix structure, where each variation is augmented with
        specific extra parameters by the way of matching parameters in the `include`
        directive.

        ```{code-block} python
        {
            "os": [
                "ubuntu-24.04-arm",
                "ubuntu-24.04",
                "macos-26",
                "macos-26-intel",
                "windows-11-arm",
                "windows-2025",
            ],
            "entry_point": [
                "mpm",
            ],
            "commit": [
                "346ce664f055fbd042a25ee0b7e96702e95",
                "6f27db47612aaee06fdf08744b09a9f5f6c2",
            ],
            "include": [
                {
                    "target": "linux-arm64",
                    "os": "ubuntu-24.04-arm",
                    "platform_id": "linux",
                    "arch": "arm64",
                    "extension": "bin",
                },
                {
                    "target": "linux-x64",
                    "os": "ubuntu-24.04",
                    "platform_id": "linux",
                    "arch": "x64",
                    "extension": "bin",
                },
                {
                    "target": "macos-arm64",
                    "os": "macos-26",
                    "platform_id": "macos",
                    "arch": "arm64",
                    "extension": "bin",
                },
                {
                    "target": "macos-x64",
                    "os": "macos-26-intel",
                    "platform_id": "macos",
                    "arch": "x64",
                    "extension": "bin",
                },
                {
                    "target": "windows-arm64",
                    "os": "windows-11-arm",
                    "platform_id": "windows",
                    "arch": "arm64",
                    "extension": "exe",
                },
                {
                    "target": "windows-x64",
                    "os": "windows-2025",
                    "platform_id": "windows",
                    "arch": "x64",
                    "extension": "exe",
                },
                {
                    "entry_point": "mpm",
                    "cli_id": "mpm",
                    "module_id": "meta_package_manager.__main__",
                    "callable_id": "main",
                    "module_path": "meta_package_manager",
                },
                {
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "short_sha": "346ce66",
                    "current_version": "2.0.0",
                },
                {
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "short_sha": "6f27db4",
                    "current_version": "1.9.1",
                },
                {
                    "os": "ubuntu-24.04-arm",
                    "entry_point": "mpm",
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "bin_name": "mpm-2.0.0-linux-arm64.bin",
                },
                {
                    "os": "ubuntu-24.04-arm",
                    "entry_point": "mpm",
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "bin_name": "mpm-1.9.1-linux-arm64.bin",
                },
                {
                    "os": "ubuntu-24.04",
                    "entry_point": "mpm",
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "bin_name": "mpm-2.0.0-linux-x64.bin",
                },
                {
                    "os": "ubuntu-24.04",
                    "entry_point": "mpm",
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "bin_name": "mpm-1.9.1-linux-x64.bin",
                },
                {
                    "os": "macos-26",
                    "entry_point": "mpm",
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "bin_name": "mpm-2.0.0-macos-arm64.bin",
                },
                {
                    "os": "macos-26",
                    "entry_point": "mpm",
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "bin_name": "mpm-1.9.1-macos-arm64.bin",
                },
                {
                    "os": "macos-26-intel",
                    "entry_point": "mpm",
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "bin_name": "mpm-2.0.0-macos-x64.bin",
                },
                {
                    "os": "macos-26-intel",
                    "entry_point": "mpm",
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "bin_name": "mpm-1.9.1-macos-x64.bin",
                },
                {
                    "os": "windows-11-arm",
                    "entry_point": "mpm",
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "bin_name": "mpm-2.0.0-windows-arm64.exe",
                },
                {
                    "os": "windows-11-arm",
                    "entry_point": "mpm",
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "bin_name": "mpm-1.9.1-windows-arm64.exe",
                },
                {
                    "os": "windows-2025",
                    "entry_point": "mpm",
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "bin_name": "mpm-2.0.0-windows-x64.exe",
                },
                {
                    "os": "windows-2025",
                    "entry_point": "mpm",
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "bin_name": "mpm-1.9.1-windows-x64.exe",
                },
                {
                    "state": "stable",
                },
            ],
        }
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

        matrix = Matrix()

        # Register all runners on which we want to run Nuitka builds.
        matrix.add_variation(
            "os", tuple(map(itemgetter("os"), NUITKA_BUILD_TARGETS.values()))
        )
        # Augment each "os" entry with platform-specific data.
        for target_data in FLAT_BUILD_TARGETS:
            matrix.add_includes(target_data)

        # Collect Nuitka flags: the standard [tool.nuitka] section first (which
        # Nuitka itself reads only when it is the build backend, not for the CLI
        # onefile build run here), then [tool.repomatic] nuitka.extra-args, then
        # any auto-detected flags appended below.
        nuitka_table = self.pyproject_toml.get("tool", {}).get("nuitka", {})
        nuitka_extra_args_list = [
            *nuitka_args_from_pyproject(nuitka_table),
            *self.config.nuitka_extra_args,
        ]

        # Filter entry points to those selected for Nuitka compilation.
        selected = set(self.nuitka_entry_points)
        for cli_id, module_id, callable_id in self.script_entries:
            if cli_id not in selected:
                continue
            # CLI ID is supposed to be unique, we'll use that as a key.
            matrix.add_variation("entry_point", [cli_id])
            # Derive CLI module path from its ID. Nuitka 4.1's
            # ``--main-entry-point`` flag is unusable on its own: it skips
            # populating ``_main_paths`` in ``nuitka.importing.Importing`` and
            # crashes with ``NuitkaCodeDeficit: Error, cannot locate modules
            # before import mechanism is setup`` inside
            # ``setStandardLibraryModules``. Falling back to a positional module
            # path keeps ``_main_paths`` initialized via
            # ``addMainScriptDirectory``.
            module_path = Path(f"{module_id.replace('.', '/')}.py")
            assert module_path.exists()

            # When the entry point is a `__main__.py` inside a package,
            # Nuitka expects the package directory (not the file) along
            # with `--python-flag=-m`.  Passing the file directly
            # produces a binary that silently exits without output.
            if module_path.name == "__main__.py":
                package_dir = module_path.parent
                init_file = package_dir / "__init__.py"
                if init_file.exists():
                    module_path = package_dir
                    nuitka_extra_args_list.append("--python-flag=-m")

            matrix.add_includes({
                "entry_point": cli_id,
                "cli_id": cli_id,
                "module_id": module_id,
                "callable_id": callable_id,
                "module_path": str(module_path),
            })

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

        # Augment each variation set of the matrix with a the binary name to be
        # produced by Nuitka. Itererate over all matrix variation sets so we have all
        # metadata necessary to generate a unique name specific to these variations.
        for variations in matrix.solve():
            # We will re-attach back this binary name to the with an include directive,
            # so we need a copy the main variants it corresponds to.
            bin_name_include = {k: variations[k] for k in matrix.variations}
            bin_name_include["bin_name"] = (
                "{cli_id}-{current_version}-{target}.{extension}"
            ).format(**variations)
            matrix.add_includes(bin_name_include)

        # Serialize the Nuitka flags as a JSON array so the release workflow can
        # rebuild an argv list with `jq`, preserving values that contain spaces
        # (e.g. --product-name="My App") that a space-joined string would split.
        nuitka_extra_args = json.dumps(nuitka_extra_args_list)
        matrix.add_includes({"nuitka_extra_args": nuitka_extra_args})

        # All jobs are stable by default, unless marked otherwise by specific
        # configuration.
        matrix.add_includes({"state": "stable"})
        for unstable_target in self.unstable_targets:
            matrix.add_includes({
                "state": "unstable",
                "os": NUITKA_BUILD_TARGETS[unstable_target]["os"],
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
        # Drop excludes that became no-ops after replace/remove changed the axes.
        matrix.prune()

    @cached_property
    def test_matrix(self) -> Matrix:
        """Full test matrix for non-PR events.

        Combines all runner OS images and Python versions, excluding known
        incompatible combinations. Marks development Python versions as
        unstable so CI can use `continue-on-error`. Per-project config
        from `[tool.repomatic.test-matrix]` is applied last.
        """
        matrix = Matrix()
        matrix.add_variation("os", TEST_RUNNERS_FULL)
        matrix.add_variation("python-version", TEST_PYTHON_FULL)
        # Python 3.10 has no native ARM64 Windows build.
        matrix.add_excludes({"os": "windows-11-arm", "python-version": "3.10"})
        matrix.add_includes({"state": "stable"})
        for version in sorted(UNSTABLE_PYTHON_VERSIONS):
            matrix.add_includes({"state": "unstable", "python-version": version})
        self._apply_test_matrix_config(matrix, full=True)
        return matrix

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
    def stale_test_matrix_excludes(self) -> list[dict[str, str]]:
        """User `test-matrix.exclude` entries matching no full-matrix axis value.

        An exclude naming a value absent from every axis (like a renamed
        runner) can never match a combination, so `Matrix.prune()` drops it
        silently and its exclusion intent is lost. This drift is common after
        an upstream runner rename (such as `macos-15-intel` becoming
        `macos-26-intel`). The `lint-repo` check surfaces these so the drift
        fails loudly instead of silently.

        :return: The offending exclude entries, in config order.
        """
        axes = self.test_matrix.all_variations()
        return [
            entry
            for entry in self.config.test_matrix.exclude
            if any(
                key not in axes or value not in axes[key]
                for key, value in entry.items()
            )
        ]

    @cached_property
    def release_notes(self) -> str | None:
        """Generate notes to be attached to the GitHub release.

        Renders the `github-releases` template with changelog
        content for the version. The template is the single place
        that defines the release body layout.
        """
        # Lazy import to avoid circular dependency:
        # release_sync → pr_body → metadata.
        from .github.release_sync import build_expected_body

        version = self.released_version
        if not version:
            version = self.current_version
        if not version:
            return None

        changelog_path = Path(self.config.changelog_location)
        if not changelog_path.exists():
            return None

        changelog = Changelog(
            changelog_path.read_text(encoding="UTF-8"),
        )
        notes = build_expected_body(changelog, version)
        return notes or None

    @cached_property
    def release_notes_with_admonition(self) -> str | None:
        """Generate release notes with a pre-computed availability admonition.

        Builds the same body as {attr}`release_notes`, but injects a
        `> [!NOTE]` admonition linking to PyPI and GitHub even before
        `fix-changelog` has a chance to update `changelog.md`.  This
        lets the `create-release` workflow step include the admonition
        at creation time when `publish-pypi` succeeds.

        Returns `None` when the project is not on PyPI, has no
        changelog, or has no version to release.
        """
        # Lazy import to avoid circular dependency:
        # release_sync → pr_body → metadata.
        from .github.release_sync import build_expected_body

        version = self.released_version
        if not version:
            version = self.current_version
        if not version or not self.package_name:
            return None

        changelog_path = Path(self.config.changelog_location)
        if not changelog_path.exists():
            return None

        changelog = Changelog(
            changelog_path.read_text(encoding="UTF-8"),
        )

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
                raise NotImplementedError

            elif isinstance(value, Iterable):
                # Cast all items to strings, wrapping Path items with double-quotes.
                if all(isinstance(i, (str, Path)) for i in value):
                    items = (
                        (f'"{i}"' if isinstance(i, Path) else str(i)) for i in value
                    )
                    value = " ".join(items)
                # XXX We only support iterables of dict[str, str] for now.
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
                raise NotImplementedError(f"GitHub formatting for: {value!r}")

        return str(value)

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
        system scans, build matrix expansion).
        """
        # Lazy factories: each value is computed only when the key is included.
        # This lets ``keys=("is_python_project",)`` skip ``nuitka_matrix`` and
        # the pydriller ``Repository(...)`` walk it pulls in.
        factories: dict[str, Callable[[], Any]] = {
            "is_bot": lambda: self.is_bot,
            "skip_binary_build": lambda: self.skip_binary_build,
            "yaml_changed": lambda: self.yaml_changed,
            "zsh_changed": lambda: self.zsh_changed,
            "workflows_changed": lambda: self.workflows_changed,
            "new_commits": lambda: self.new_commits_hash,
            "release_commits": lambda: self.release_commits_hash,
            "mailmap_exists": lambda: self.mailmap_exists,
            "gitignore_exists": lambda: self.gitignore_exists,
            "renovate_config_exists": lambda: self.renovate_config_exists,
            "python_files": lambda: self.python_files,
            "json_files": lambda: self.json_files,
            "yaml_files": lambda: self.yaml_files,
            "toml_files": lambda: self.toml_files,
            "pyproject_files": lambda: self.pyproject_files,
            "workflow_files": lambda: self.workflow_files,
            "doc_files": lambda: self.doc_files,
            "markdown_files": lambda: self.markdown_files,
            "image_files": lambda: self.image_files,
            "shfmt_files": lambda: self.shfmt_files,
            "zsh_files": lambda: self.zsh_files,
            "is_python_project": lambda: self.is_python_project,
            "package_name": lambda: self.package_name,
            "cli_scripts": lambda: [cli_id for cli_id, _, _ in self.script_entries],
            "project_description": lambda: self.project_description,
            "mypy_params": lambda: self.mypy_params,
            "current_version": lambda: self.current_version,
            "released_version": lambda: self.released_version,
            "is_sphinx": lambda: self.is_sphinx,
            "active_autodoc": lambda: self.active_autodoc,
            "uses_myst": lambda: self.uses_myst,
            "release_notes": lambda: self.release_notes,
            "release_notes_with_admonition": lambda: self.release_notes_with_admonition,
            "new_commits_matrix": lambda: self.new_commits_matrix,
            "release_commits_matrix": lambda: self.release_commits_matrix,
            "build_targets": lambda: FLAT_BUILD_TARGETS,
            "nuitka_matrix": lambda: self.nuitka_matrix,
            "test_matrix": lambda: self.test_matrix,
            "test_matrix_pr": lambda: self.test_matrix_pr,
            "minor_bump_allowed": lambda: self.minor_bump_allowed,
            "major_bump_allowed": lambda: self.major_bump_allowed,
        }

        # Add config from [tool.repomatic] in pyproject.toml.
        # Convert kebab-case config keys to snake_case metadata keys.
        # Exclude nuitka internal config (dedicated properties with validation logic)
        # and subcommand config fields (read directly by test-plan and deps-graph).
        for f in fields(Config):
            if (
                f.name
                not in (
                    "nuitka_entry_points",
                    "nuitka_extra_args",
                    "nuitka_unstable_targets",
                )
                and f.name not in SUBCOMMAND_CONFIG_FIELDS
            ):
                # Bind ``f.name`` to ``name`` to avoid late-binding closure pitfalls.
                factories[f.name] = lambda name=f.name: getattr(self.config, name)  # type: ignore[misc]

        keys_set = set(keys)
        metadata: dict[str, Any] = {
            name: factory()
            for name, factory in factories.items()
            if not keys_set or name in keys_set
        }

        logging.debug(f"Raw metadata: {metadata!r}")
        logging.debug(f"Format metadata into {dialect} format.")

        content = ""
        if dialect == Dialect.github:
            for env_name, value in metadata.items():
                env_value = self.format_github_value(value)

                # Use heredoc format for multiline values or fields with special chars.
                use_heredoc = (
                    len(env_value.splitlines()) > 1 or env_name in HEREDOC_FIELDS
                )
                if not use_heredoc:
                    content += f"{env_name}={env_value}\n"
                else:
                    # Use a random unique delimiter to encode multiline value.
                    delimiter = generate_delimiter()
                    content += f"{env_name}<<{delimiter}\n{env_value}\n{delimiter}\n"
        elif dialect == Dialect.github_json:
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
                    formatted[k] = self.format_github_value(v)
                else:
                    formatted[k] = v
            json_str = json.dumps(formatted, cls=JSONMetadata, separators=(",", ":"))
            content = f"metadata={json_str}\n"
        else:
            assert dialect == Dialect.json
            content = json.dumps(metadata, cls=JSONMetadata, indent=2)

        logging.debug(f"Formatted metadata:\n{content}")

        return content
