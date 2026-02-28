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
exposes ``github.event.head_commit`` (the most recent commit), but workflows often need
to process all commits in the push event.

This is critical for releases, where two commits are pushed together:

1. ``[changelog] Release vX.Y.Z`` — the release commit to be tagged and published
2. ``[changelog] Post-release bump vX.Y.Z → vX.Y.Z`` — bumps version for the next dev cycle

Since ``github.event.head_commit`` only sees the post-release bump, this module extracts
the full commit range from the push event and identifies release commits that need
special handling (tagging, PyPI publishing, GitHub release creation).

The following variables are `printed to the environment file
<https://docs.github.com/en/free-pro-team@latest/actions/reference/workflow-commands-for-github-actions#environment-files>`_:

```text
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
        "os": "macos-15-intel",
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
        "macos-15-intel",
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
            "os": "macos-15-intel",
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
            "module_path": "meta_package_manager/__main__.py"
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
            "os": "macos-15-intel",
            "entry_point": "mpm",
            "commit": "346ce664f055fbd042a25ee0b7e96702e95",
            "bin_name": "mpm-macos-x64.bin"
        },
        {
            "os": "macos-15-intel",
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

.. warning::
    Fields with serialized lists and dictionaries, like ``new_commits_matrix``,
    ``build_targets`` or ``nuitka_matrix``, are pretty-printed in the example above for
    readability. They are inlined in the actual output and not formatted this way.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from functools import cached_property
from operator import itemgetter
from pathlib import Path
from textwrap import dedent

from bumpversion.config import get_configuration  # type: ignore[import-untyped]
from bumpversion.config.files import find_config_file  # type: ignore[import-untyped]
from bumpversion.show import resolve_name  # type: ignore[import-untyped]
from extra_platforms import is_github_ci
from gitdb.exc import BadName  # type: ignore[import-untyped]
from packaging.version import Version
from py_walk import get_parser_from_file
from py_walk.models import Parser
from pydriller import Commit, Git, Repository  # type: ignore[import-untyped]
from pyproject_metadata import ConfigurationError, StandardMetadata
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

from .changelog import Changelog
from .github.actions import NULL_SHA, WorkflowEvent, generate_delimiter
from .github.matrix import Matrix

if sys.version_info >= (3, 11):
    import tomllib
    from enum import StrEnum
else:
    import tomli as tomllib  # type: ignore[import-not-found]
    from backports.strenum import StrEnum  # type: ignore[import-not-found]

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final, Literal


class Dialect(StrEnum):
    """Output dialect for metadata serialization."""

    github = "github"
    json = "json"


@dataclass
class Config:
    """Configuration schema for ``[tool.repomatic]`` in ``pyproject.toml``.

    This dataclass defines the structure and default values for repomatic configuration.
    Each field has a docstring explaining its purpose.
    """

    nuitka: bool = True
    """Whether Nuitka binary compilation is enabled for this project.

    Projects with ``[project.scripts]`` entries that are not intended to produce
    standalone binaries (e.g., libraries with convenience CLI wrappers) can set this
    to ``false`` to opt out of Nuitka compilation.
    """

    nuitka_extra_args: list[str] = field(default_factory=list)
    """Extra Nuitka CLI arguments for binary compilation.

    Project-specific flags (e.g., ``--include-data-files``,
    ``--include-package-data``) that are passed to the Nuitka build command.
    """

    pypi_package_history: list[str] = field(default_factory=list)
    """Former PyPI package names for projects that were renamed.

    When a project changes its PyPI name, older versions remain published under
    the previous name. List former names here so ``lint-changelog`` can fetch
    release metadata from all names and generate correct PyPI URLs.
    """

    unstable_targets: list[str] = field(default_factory=list)
    """Nuitka build targets allowed to fail without blocking the release.

    List of target names (e.g., ``["linux-arm64", "windows-x64"]``) that are marked as
    unstable. Jobs for these targets will be allowed to fail without preventing the
    release workflow from succeeding.
    """

    renovate_sync: bool = True
    """Whether Renovate config sync is enabled for this project.

    Projects that manage their own ``renovate.json5`` and do not want the
    autofix job to overwrite it can set this to ``false``.
    """

    workflow_sync: bool = True
    """Whether workflow sync is enabled for this project.

    Projects that manage their own workflow files and do not want the autofix job
    to sync thin callers or headers can set this to ``false``.
    """

    init_exclude: list[str] = field(
        default_factory=lambda: ["labels", "linters", "skills"],
    )
    """Component names to exclude from ``repomatic init`` default selection.

    Defaults to ``["labels", "linters", "skills"]`` because these files are
    regenerated by their respective workflows at execution time, making their
    presence in the repository unnecessary.

    Each entry is a component name (e.g., ``"linters"``, ``"skills"``) that will be
    skipped when running ``repomatic init`` without explicit positional arguments.
    Explicit CLI positional arguments override this list.
    """

    workflow_sync_exclude: list[str] = field(default_factory=list)
    """Workflow filenames to exclude from ``repomatic init``, ``workflow sync``, and
    ``workflow create``.

    Each entry is a workflow filename (e.g., ``"debug.yaml"``) that will be skipped
    when initializing, syncing, or creating workflow files without explicit positional
    arguments. Explicit CLI positional arguments override this list.
    """

    test_plan_file: str = "./tests/cli-test-plan.yaml"
    """Path to the YAML test plan file for binary testing.

    The test plan file defines a list of test cases to run against compiled binaries.
    Each test case specifies command-line arguments and expected output patterns.
    """

    timeout: int | None = None
    """Timeout in seconds for each binary test.

    If set, each test command will be terminated after this duration. ``None`` means no
    timeout (tests can run indefinitely).
    """

    test_plan: str | None = None
    """Inline YAML test plan for binaries.

    Alternative to ``test_plan_file``. Allows specifying the test plan directly in
    ``pyproject.toml`` instead of a separate file.
    """

    gitignore_location: str = "./.gitignore"
    """File path of the ``.gitignore`` to update, relative to the root of the repository.
    """

    gitignore_extra_categories: list[str] = field(default_factory=list)
    """Additional gitignore template categories to fetch from gitignore.io.

    List of template names (e.g., ``["Python", "Node", "Terraform"]``) to combine
    with the generated ``.gitignore`` content.
    """

    gitignore_extra_content: str = field(
        default_factory=lambda: dedent(
            """
            junit.xml

            # Claude Code local settings.
            .claude/settings.local.json
            """
        ).strip()
    )
    """Additional content to append at the end of the generated ``.gitignore`` file.
    """

    dependency_graph_output: str = "./docs/assets/dependencies.mmd"
    """Path where the dependency graph Mermaid diagram should be written.

    The dependency graph visualizes the project's dependency tree in Mermaid format.
    """

    dependency_graph_all_groups: bool = True
    """Whether to include all dependency groups in the graph.

    When ``True``, the ``update-deps-graph`` command behaves as if
    ``--all-groups`` was passed. Projects that want to exclude development
    dependency groups (docs, test, typing) from their published graph can
    set this to ``false``.
    """

    dependency_graph_all_extras: bool = True
    """Whether to include all optional extras in the graph.

    When ``True``, the ``update-deps-graph`` command behaves as if
    ``--all-extras`` was passed.
    """

    dependency_graph_no_groups: list[str] = field(default_factory=list)
    """Dependency groups to exclude from the graph.

    Equivalent to passing ``--no-group`` for each entry. Takes precedence
    over ``dependency-graph-all-groups``.
    """

    dependency_graph_no_extras: list[str] = field(default_factory=list)
    """Optional extras to exclude from the graph.

    Equivalent to passing ``--no-extra`` for each entry. Takes precedence
    over ``dependency-graph-all-extras``.
    """

    dependency_graph_level: int | None = None
    """Maximum depth of the dependency graph.

    ``None`` means unlimited. ``1`` = primary deps only, ``2`` = primary +
    their deps, etc. Equivalent to ``--level``.
    """

    extra_label_files: list[str] = field(default_factory=list)
    """URLs of additional label definition files (JSON, JSON5, TOML, or YAML).

    Each URL is downloaded and applied separately by ``labelmaker``.
    """

    extra_file_rules: str = ""
    """Additional YAML rules appended to the file-based labeller configuration.

    Appended to the bundled ``labeller-file-based.yaml`` during export.
    """

    extra_content_rules: str = ""
    """Additional YAML rules appended to the content-based labeller configuration.

    Appended to the bundled ``labeller-content-based.yaml`` during export.
    """


SUBCOMMAND_CONFIG_FIELDS: Final[frozenset[str]] = frozenset((
    "dependency_graph_all_extras",
    "dependency_graph_all_groups",
    "dependency_graph_level",
    "dependency_graph_no_extras",
    "dependency_graph_no_groups",
    "dependency_graph_output",
    "extra_content_rules",
    "extra_file_rules",
    "extra_label_files",
    "gitignore_extra_categories",
    "gitignore_extra_content",
    "gitignore_location",
    "init_exclude",
    "pypi_package_history",
    "renovate_sync",
    "test_plan",
    "test_plan_file",
    "timeout",
    "workflow_sync",
    "workflow_sync_exclude",
))
"""Config fields consumed directly by subcommands, not needed as metadata outputs.

The ``test-plan`` and ``deps-graph`` subcommands now read these values directly from
``[tool.repomatic]`` in ``pyproject.toml``, so they no longer need to be passed through
workflow metadata outputs.
"""


def load_repomatic_config(
    pyproject_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load ``[tool.repomatic]`` config merged with ``Config`` defaults.

    :param pyproject_data: Pre-parsed ``pyproject.toml`` dict. If ``None``,
        reads and parses ``pyproject.toml`` from the current working directory.
    """
    if pyproject_data is None:
        pyproject_path = Path() / "pyproject.toml"
        if pyproject_path.exists() and pyproject_path.is_file():
            pyproject_data = tomllib.loads(pyproject_path.read_text(encoding="UTF-8"))
        else:
            pyproject_data = {}

    user_config: dict[str, Any] = pyproject_data.get("tool", {}).get("repomatic", {})
    schema = Config()
    config = {f.name.replace("_", "-"): getattr(schema, f.name) for f in fields(Config)}
    config.update(user_config)
    return config


def get_project_name(
    pyproject_data: dict[str, Any] | None = None,
) -> str | None:
    """Read the project name from ``pyproject.toml``.

    :param pyproject_data: Pre-parsed dict. If ``None``, reads from CWD.
    """
    if pyproject_data is None:
        pyproject_path = Path() / "pyproject.toml"
        if not (pyproject_path.exists() and pyproject_path.is_file()):
            return None
        pyproject_data = tomllib.loads(pyproject_path.read_text(encoding="UTF-8"))
    name: str | None = pyproject_data.get("project", {}).get("name")
    return name


SHORT_SHA_LENGTH = 7
"""Default SHA length hard-coded to ``7``.

.. caution::

    The `default is subject to change <https://stackoverflow.com/a/21015031>`_ and
    depends on the size of the repository.
"""

RELEASE_COMMIT_PATTERN = re.compile(r"^\[changelog\] Release v[0-9]+\.[0-9]+\.[0-9]+$")
"""Pre-compiled regex pattern for identifying release commits.

A rebase merge preserves the original commit messages, so release commits
match this pattern. A squash merge replaces them with the PR title
(e.g. ``Release `v1.2.3` (#42)``), which does **not** match. This mismatch
is the mechanism by which squash merges are safely skipped: the ``create-tag``
job only processes commits matching this pattern, so no tag, PyPI publish, or
GitHub release is created from a squash merge. The ``detect-squash-merge``
job in ``release.yaml`` detects this and opens an issue to notify the
maintainer.
"""


BINARY_AFFECTING_PATHS: Final[tuple[str, ...]] = (
    ".github/workflows/release.yaml",
    "pyproject.toml",
    "tests/",
    "uv.lock",
)
"""Path prefixes that always affect compiled binaries, regardless of the project.

Project-specific source directories (derived from ``[project.scripts]`` in
``pyproject.toml``) are added dynamically by
:attr:`Metadata.binary_affecting_paths`.
"""

SKIP_BINARY_BUILD_BRANCHES: Final[frozenset[str]] = frozenset((
    # Autofix branches that don't affect compiled binaries.
    "format-json",
    "format-markdown",
    "optimize-images",
    "sync-gitignore",
    "sync-mailmap",
    "update-deps-graph",
))
"""Branch names for which binary builds should be skipped.

These branches contain changes that do not affect compiled binaries:

- ``.mailmap`` updates only affect contributor attribution
- Documentation and image changes don't affect code
- ``.gitignore`` and JSON config changes don't affect binaries

This allows workflows to skip expensive Nuitka compilation jobs for PRs that cannot
possibly change the binary output.
"""

HEREDOC_FIELDS: Final[frozenset[str]] = frozenset((
    # Contains markdown with brackets, parentheses, and emojis that can break parsing.
    "release_notes",
    "release_notes_with_admonition",
))
"""Metadata fields that should always use heredoc format in GitHub Actions output.

Some fields may contain special characters (brackets, parentheses, emojis, or potential
newlines) that can break GitHub Actions parsing when using simple ``key=value`` format.
These fields will use the heredoc delimiter format regardless of whether they currently
contain multiple lines.
"""

MAILMAP_PATH = Path(".mailmap")

GITIGNORE_PATH = Path(".gitignore")

RENOVATE_CONFIG_PATH = Path("renovate.json5")

NUITKA_BUILD_TARGETS = {
    "linux-arm64": {
        "os": "ubuntu-24.04-arm",
        "platform_id": "linux",
        "arch": "arm64",
        "extension": "bin",
    },
    "linux-x64": {
        "os": "ubuntu-24.04",
        "platform_id": "linux",
        "arch": "x64",
        "extension": "bin",
    },
    "macos-arm64": {
        "os": "macos-26",
        "platform_id": "macos",
        "arch": "arm64",
        "extension": "bin",
    },
    "macos-x64": {
        "os": "macos-15-intel",
        "platform_id": "macos",
        "arch": "x64",
        "extension": "bin",
    },
    "windows-arm64": {
        "os": "windows-11-arm",
        "platform_id": "windows",
        "arch": "arm64",
        "extension": "exe",
    },
    "windows-x64": {
        "os": "windows-2025",
        "platform_id": "windows",
        "arch": "x64",
        "extension": "exe",
    },
}
"""List of GitHub-hosted runners used for Nuitka builds.

The key of the dictionary is the target name, which is used as a short name for
user-friendlyness. As such, it is used to name the compiled binary.

Values are dictionaries with the following keys:

- ``os``: Operating system name, as used in `GitHub-hosted runners
    <https://docs.github.com/en/actions/writing-workflows/choosing-where-your-workflow-runs/choosing-the-runner-for-a-job#standard-github-hosted-runners-for-public-repositories>`_.

    .. hint::
        We choose to run the compilation only on the latest supported version of each
        OS, for each architecture. Note that macOS and Windows do not have the latest
        version available for each architecture.

- ``platform_id``: Platform identifier, as defined by `Extra Platform
  <https://github.com/kdeldycke/extra-platforms>`_.

- ``arch``: Architecture identifier.

    .. note::
        Architecture IDs are `inspired from those specified for self-hosted runners
        <https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/supported-architectures-and-operating-systems-for-self-hosted-runners#supported-processor-architectures>`_

    .. note::
        Maybe we should just adopt `target triple
        <https://mcyoung.xyz/2025/04/14/target-triples/>`_.

- ``extension``: File extension of the compiled binary.
"""


FLAT_BUILD_TARGETS = [
    {"target": target_id} | target_data
    for target_id, target_data in NUITKA_BUILD_TARGETS.items()
]
"""List of build targets in a flat format, suitable for matrix inclusion."""


MYPY_VERSION_MIN: Final = (3, 8)
"""Earliest version supported by Mypy's ``--python-version 3.x`` parameter.

`Sourced from Mypy original implementation
<https://github.com/python/mypy/blob/master/mypy/defaults.py>`_.
"""


# Silence overly verbose debug messages from py-walk logger.
logging.getLogger("py_walk").setLevel(logging.WARNING)


def get_latest_tag_version() -> Version | None:
    """Returns the latest release version from Git tags.

    Looks for tags matching the pattern ``vX.Y.Z`` and returns the highest version.
    Returns ``None`` if no matching tags are found.
    """
    git = Git(".")
    # Get all tags matching the version pattern.
    tags = git.repo.git.tag("--list", "v[0-9]*.[0-9]*.[0-9]*").splitlines()

    if not tags:
        logging.debug("No version tags found in repository.")
        return None

    # Parse and find the highest version.
    versions = []
    for tag in tags:
        # Strip the 'v' prefix and parse.
        version = Version(tag.lstrip("v"))
        versions.append(version)

    latest = max(versions)
    logging.debug(f"Latest tag version: {latest}")
    return latest


def get_release_version_from_commits(max_count: int = 10) -> Version | None:
    """Extract release version from recent commit messages.

    Searches recent commits for messages matching the pattern
    ``[changelog] Release vX.Y.Z`` and returns the version from the most recent match.

    This provides a fallback when tags haven't been pushed yet due to race conditions
    between workflows. The release commit message contains the version information
    before the tag is created.

    :param max_count: Maximum number of commits to search.
    :return: The version from the most recent release commit, or ``None`` if not found.
    """
    git = Git(".")
    release_pattern = re.compile(r"^\[changelog\] Release v(\d+\.\d+\.\d+)")

    for commit in git.repo.iter_commits("HEAD", max_count=max_count):
        match = release_pattern.match(commit.message)
        if match:
            version = Version(match.group(1))
            logging.debug(f"Found release version {version} in commit {commit.hexsha}")
            return version

    logging.debug("No release commit found in recent history.")
    return None


def is_version_bump_allowed(part: "Literal['minor', 'major']") -> bool:
    """Check if a version bump of the specified part is allowed.

    This prevents double version increments within a development cycle. A bump is
    blocked if the version has already been bumped (but not released) since the last
    tagged release.

    For example:
    - Last release: ``v5.0.1``, current: ``5.0.2`` → minor bump allowed
    - Last release: ``v5.0.1``, current: ``5.1.0`` → minor bump NOT allowed (bumped)
    - Last release: ``v5.0.1``, current: ``6.0.0`` → major bump NOT allowed (bumped)

    .. note::
        When tags are not available (e.g., due to race conditions between workflows),
        this function falls back to parsing version from recent commit messages.

    :param part: The version part to check (``minor`` or ``major``).
    :return: ``True`` if the bump should proceed, ``False`` if it should be skipped.
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
    """Metadata class."""

    def __init__(self) -> None:
        """Initialize internal variables."""

    pyproject_path = Path() / "pyproject.toml"
    sphinx_conf_path = Path() / "docs" / "conf.py"

    @cached_property
    def github_event(self) -> dict[str, Any]:
        """Load the GitHub event payload from ``GITHUB_EVENT_PATH``.

        GitHub Actions automatically sets ``GITHUB_EVENT_PATH`` to a JSON file
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
        """Deepen a shallow clone until the provided ``commit_hash`` is found.

        Progressively fetches more commits from the current repository until the
        specified commit is found or max attempts is reached.

        Returns ``True`` if the commit was found, ``False`` otherwise.
        """
        # Cache the current depth to avoid repeated subprocess calls.
        current_depth: int | None = None

        for attempt in range(max_attempts):
            try:
                _ = self.git.get_commit(commit_hash)
                if attempt > 0:
                    logging.info(
                        f"Found commit {commit_hash} after {attempt} deepen "
                        "operation(s)."
                    )
                return True
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
                except Exception as ex:
                    logging.error(f"Failed to deepen repository: {ex}")
                    return False

        return False

    def commit_matrix(self, commits: Iterable[Commit] | None) -> Matrix | None:
        """Pre-compute a matrix of commits.

        .. danger::
            This method temporarily modify the state of the repository to compute
            version metadata from the past.

            To prevent any loss of uncommitted data, it stashes and unstash the
            local changes between checkouts.

        The list of commits is augmented with long and short SHA values, as well as
        current version. Most recent commit is first, oldest is last.

        Returns a ready-to-use matrix structure:

        .. code-block:: python
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
            # reference is either the canonical active branch name (i.e. ``main``), or
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
    def event_type(self) -> "WorkflowEvent | None":
        """Returns the type of event that triggered the workflow run.

        .. caution::
            This property is based on a crude heuristics as it only looks at the value
            of the ``GITHUB_BASE_REF`` environment variable. Which is `only set when
            the event that triggers a workflow run is either pull_request or
            pull_request_target
            <https://docs.github.com/en/actions/learn-github-actions/variables#default-environment-variables>`_.

        .. todo::
            Add detection of all workflow trigger events.
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
            return WorkflowEvent.pull_request  # type: ignore[return-value]
        return WorkflowEvent.push  # type: ignore[return-value]

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
        """Returns ``True`` if the workflow was triggered by a bot or automated process.

        This is useful to only run some jobs on human-triggered events. Or skip jobs
        triggered by bots to avoid infinite loops.

        Also detects Renovate PRs by branch name pattern (``renovate/*``), which handles
        cases where Renovate runs as a user account rather than the ``renovate[bot]`` app.
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
        if self.head_branch and self.head_branch.startswith("renovate/"):
            return True
        return False

    @cached_property
    def head_branch(self) -> str | None:
        """Returns the head branch name for pull request events.

        For pull request events, this is the source branch name
        (e.g., ``update-mailmap``). For push events, returns ``None`` since
        there's no head branch concept.

        The branch name is extracted from the ``GITHUB_HEAD_REF`` environment variable,
        which is `only set for pull request events
        <https://docs.github.com/en/actions/learn-github-actions/variables>`_.
        """
        head_ref = os.environ.get("GITHUB_HEAD_REF")
        if head_ref:
            return head_ref
        return None

    @cached_property
    def changed_files(self) -> tuple[str, ...] | None:
        """Returns the list of files changed in the current event's commit range.

        Uses ``git diff --name-only`` between the start and end of the commit range.
        Returns ``None`` if no commit range is available (e.g., outside CI).
        """
        if not self.commit_range:
            return None
        start, end = self.commit_range
        if not start or not end:
            return None
        try:
            diff_output = self.git.repo.git.diff("--name-only", start, end)
        except Exception:
            logging.warning("Failed to get changed files from git diff.")
            return None
        if not diff_output:
            return ()
        return tuple(diff_output.strip().splitlines())

    @cached_property
    def binary_affecting_paths(self) -> tuple[str, ...]:
        """Path prefixes that affect compiled binaries for this project.

        Combines the static :data:`BINARY_AFFECTING_PATHS` (common files like
        ``pyproject.toml``, ``uv.lock``, ``tests/``) with project-specific source
        directories derived from ``[project.scripts]`` in ``pyproject.toml``.

        For example, a project with ``mpm = "meta_package_manager.__main__:main"``
        adds ``meta_package_manager/`` as an affecting path. This makes the check
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
    def skip_binary_build(self) -> bool:
        """Returns ``True`` if binary builds should be skipped for this event.

        Binary builds are expensive and time-consuming. This property identifies
        contexts where the changes cannot possibly affect compiled binaries,
        allowing workflows to skip Nuitka compilation jobs.

        Two mechanisms are checked:

        1. **Branch name** — PRs from known non-code branches (documentation,
           ``.mailmap``, ``.gitignore``, etc.) are skipped.
        2. **Changed files** — Push events where all changed files fall outside
           :attr:`binary_affecting_paths` are skipped. This avoids ~2h of Nuitka
           builds for documentation-only commits to ``main``.
        """
        if self.head_branch and self.head_branch in SKIP_BINARY_BUILD_BRANCHES:
            logging.info(
                f"Branch {self.head_branch!r} is in SKIP_BINARY_BUILD_BRANCHES. "
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

        1. ``[changelog] Release vX.Y.Z`` — the release commit
        2. ``[changelog] Post-release bump vX.Y.Z → vX.Y.Z`` — the post-release bump

        Without extracting the full commit range, the release commit would be missed
        since ``github.event.head_commit`` only exposes the post-release bump.

        This property also enables processing each commit individually when we want to
        keep a carefully constructed commit history. The typical example is a pull
        request that is merged upstream but we'd like to produce artifacts (builds,
        packages, etc.) for each individual commit.

        The default ``GITHUB_SHA`` environment variable is not enough as it only points
        to the last commit. We need to inspect the commit history to find all new ones.
        New commits need to be fetched differently in ``push`` and ``pull_request``
        events.

        .. seealso::
            - https://stackoverflow.com/a/67204539
            - https://stackoverflow.com/a/62953566
            - https://stackoverflow.com/a/61861763

        .. seealso::
            Pull request events on GitHub are a bit complex, see: `The Many SHAs of a
            GitHub Pull Request
            <https://www.kenmuse.com/blog/the-many-shas-of-a-github-pull-request/>`_.
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
            end = os.environ.get("GITHUB_SHA")
        logging.debug(f"Commit range: {start} -> {end}")
        if not start or not end:
            logging.warning(f"Incomplete commit range: {start} -> {end}")
        return start, end

    @cached_property
    def current_commit(self) -> Commit | None:
        """Returns the current ``Commit`` object."""
        return next(Repository(".", single="HEAD").traverse_commits())

    @cached_property
    def current_commit_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of the current commit."""
        return self.commit_matrix((self.current_commit,))

    @cached_property
    def new_commits(self) -> tuple[Commit, ...] | None:
        """Returns list of all ``Commit`` objects bundled within the triggering event.

        This extracts **all commits** from the push event, not just ``head_commit``.
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
        """Returns list of ``Commit`` objects to be tagged within the triggering event.

        This filters ``new_commits`` to find release commits that need special handling:
        tagging, PyPI publishing, and GitHub release creation.

        This is essential because when a release is pushed, ``github.event.head_commit``
        only exposes the post-release bump commit, not the release commit. By extracting
        all commits from the event (via ``new_commits``) and filtering for release
        commits here, we ensure the release workflow can properly identify and process
        the ``[changelog] Release vX.Y.Z`` commit.

        We cannot identify a release commit based on the presence of a ``vX.Y.Z`` tag
        alone. That's because the tag is not present in the ``prepare-release`` pull
        request produced by the ``changelog.yaml`` workflow. The tag is created later
        by the ``release.yaml`` workflow, when the pull request is merged to ``main``.

        Our best option is to identify a release based on the full commit message,
        using the template from the ``changelog.yaml`` workflow.
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
        return RENOVATE_CONFIG_PATH.is_file()

    @cached_property
    def gitignore_parser(self) -> Parser | None:
        """Returns a parser for the ``.gitignore`` file, if it exists."""
        if self.gitignore_exists:
            logging.debug(f"Parse {GITIGNORE_PATH}")
            return get_parser_from_file(GITIGNORE_PATH)
        return None

    def gitignore_match(self, file_path: Path | str) -> bool:
        if self.gitignore_parser and self.gitignore_parser.match(file_path):
            return True
        return False

    def glob_files(self, *patterns: str) -> list[Path]:
        """Return all file path matching the ``patterns``.

        Patterns are glob patterns supporting ``**`` for recursive search, and ``!``
        for negation.

        All directories are traversed, whether they are hidden (i.e. starting with a
        dot ``.``) or not, including symlinks.

        Skips:

        - files which does not exists
        - directories
        - broken symlinks
        - files matching patterns specified by ``.gitignore`` file

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

        .. note::
            JSON5 files are excluded because Biome doesn't support them.
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

        Covers the formats handled by ``repomatic optimize-images``: JPEG, PNG,
        WebP, and AVIF. See :mod:`repomatic.images` for the optimization tools.
        """
        return self.glob_files("**/*.{jpeg,jpg,png,webp,avif}")

    @cached_property
    def zsh_files(self) -> list[Path]:
        """Returns a list of Zsh files."""
        return self.glob_files("**/*.{sh,zsh}", "**/.{zshrc,zprofile,zshenv,zlogin}")

    @cached_property
    def is_python_project(self):
        """Returns ``True`` if repository is a Python project.

        Presence of a ``pyproject.toml`` file that respects the standards is enough
        to consider the project as a Python one.
        """
        return False if self.pyproject is None else True

    @cached_property
    def pyproject_toml(self) -> dict[str, Any]:
        """Returns the raw parsed content of ``pyproject.toml``.

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
        """Returns metadata stored in the ``pyproject.toml`` file.

        Returns ``None`` if the ``pyproject.toml`` does not exists or does not respects
        the PEP standards.

        .. warning::
            Some third-party apps have their configuration saved into
            ``pyproject.toml`` file, but that does not means the project is a Python
            one. For that, the ``pyproject.toml`` needs to respect the PEPs.
        """
        toml = self.pyproject_toml
        if toml:
            try:
                return StandardMetadata.from_pyproject(toml)
            except ConfigurationError:
                pass
        return None

    @cached_property
    def config(self) -> dict[str, Any]:
        """Returns the ``[tool.repomatic]`` section from ``pyproject.toml``.

        Merges user configuration with defaults from ``Config``.
        """
        return load_repomatic_config(self.pyproject_toml)

    @cached_property
    def unstable_targets(self) -> set[str]:
        """Nuitka build targets allowed to fail without blocking the release.

        Reads ``[tool.repomatic].unstable-targets`` from ``pyproject.toml``. Defaults
        to an empty set.

        Unrecognized target names are logged as warnings and discarded.
        """
        raw = self.config["unstable-targets"]
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

        Results are derived from the script entries of ``pyproject.toml``. So that:

        .. code-block:: toml
            [project.scripts]
            mdedup = "mail_deduplicate.cli:mdedup"
            mpm = "meta_package_manager.__main__:main"

        Will yields the following list:

        .. code-block:: python
            (
                ("mdedup", "mail_deduplicate.cli", "mdedup"),
                ("mpm", "meta_package_manager.__main__", "main"),
                ...,
            )
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
    def mypy_params(self) -> str | None:
        """Generates ``mypy`` parameters.

        Mypy needs to be fed with this parameter: ``--python-version 3.x``.

        Extracts the minimum Python version from the project's ``requires-python``
        specifier. Only takes ``major.minor`` into account.
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
        return f"--python-version {major}.{minor}"

    @staticmethod
    def get_current_version() -> str | None:
        """Returns the current version as managed by bump-my-version.

        Same as calling the CLI:

            .. code-block:: shell-session
                $ bump-my-version show current_version
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

        Current version is fetched from the ``bump-my-version`` configuration file.

        During a release, two commits are bundled into a single push event:

        1. ``[changelog] Release vX.Y.Z`` — freezes the version to the release number
        2. ``[changelog] Post-release bump vX.Y.Z → vX.Y.Z`` — bumps to the next dev version

        In this situation, the current version returned is the one from the most recent
        commit (the post-release bump), which represents the next development version.
        Use ``released_version`` to get the version from the release commit.
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
        ``[changelog] Release vX.Y.Z`` commit, which is distinct from
        ``current_version`` (the post-release bump version). This is used for
        tagging, PyPI publishing, and GitHub release creation.

        Returns ``None`` if no release commit is found in the current event.
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
        """Returns ``True`` if the Sphinx config file is present."""
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

    @cached_property
    def active_autodoc(self) -> bool:
        """Returns ``True`` if there are active Sphinx extensions."""
        if self.is_sphinx:
            # Look for list of active Sphinx extensions.
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
                            e.value
                            for e in node.value.elts
                            if isinstance(e, ast.Constant)
                        )
                        if "sphinx.ext.autodoc" in elements:
                            return True
        return False

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

        .. code-block:: python
            {
                "os": [
                    "ubuntu-24.04-arm",
                    "ubuntu-24.04",
                    "macos-26",
                    "macos-15-intel",
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
                        "os": "macos-15-intel",
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
                        "module_path": "meta_package_manager/__main__.py",
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
                        "bin_name": "mpm-linux-arm64-346ce66.bin",
                    },
                    {
                        "os": "ubuntu-24.04-arm",
                        "entry_point": "mpm",
                        "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                        "bin_name": "mpm-linux-arm64-6f27db4.bin",
                    },
                    {
                        "os": "ubuntu-24.04",
                        "entry_point": "mpm",
                        "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                        "bin_name": "mpm-linux-x64-346ce66.bin",
                    },
                    {
                        "os": "ubuntu-24.04",
                        "entry_point": "mpm",
                        "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                        "bin_name": "mpm-linux-x64-6f27db4.bin",
                    },
                    {
                        "os": "macos-26",
                        "entry_point": "mpm",
                        "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                        "bin_name": "mpm-macos-arm64-346ce66.bin",
                    },
                    {
                        "os": "macos-26",
                        "entry_point": "mpm",
                        "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                        "bin_name": "mpm-macos-arm64-6f27db4.bin",
                    },
                    {
                        "os": "macos-15-intel",
                        "entry_point": "mpm",
                        "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                        "bin_name": "mpm-macos-x64-346ce66.bin",
                    },
                    {
                        "os": "macos-15-intel",
                        "entry_point": "mpm",
                        "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                        "bin_name": "mpm-macos-x64-6f27db4.bin",
                    },
                    {
                        "os": "windows-11-arm",
                        "entry_point": "mpm",
                        "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                        "bin_name": "mpm-windows-arm64-346ce66.bin",
                    },
                    {
                        "os": "windows-11-arm",
                        "entry_point": "mpm",
                        "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                        "bin_name": "mpm-windows-arm64-6f27db4.bin",
                    },
                    {
                        "os": "windows-2025",
                        "entry_point": "mpm",
                        "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                        "bin_name": "mpm-windows-x64-346ce66.exe",
                    },
                    {
                        "os": "windows-2025",
                        "entry_point": "mpm",
                        "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                        "bin_name": "mpm-windows-x64-6f27db4.exe",
                    },
                    {
                        "state": "stable",
                    },
                ],
            }
        """
        # Only produce a matrix if the project is providing CLI entry points.
        if not self.script_entries:
            return None

        # Allow projects to opt out of Nuitka compilation via pyproject.toml.
        if not self.config["nuitka"]:
            logging.info(
                "[tool.repomatic] nuitka is disabled. Skipping binary compilation."
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

        # Augment each entry point with some metadata.
        for cli_id, module_id, callable_id in self.script_entries:
            # CLI ID is supposed to be unique, we'll use that as a key.
            matrix.add_variation("entry_point", [cli_id])
            # Derive CLI module path from its ID.
            # XXX We consider here the module is directly callable, because Nuitka
            # doesn't seems to support the entry-point notation.
            module_path = Path(f"{module_id.replace('.', '/')}.py")
            assert module_path.exists()
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
            bin_name_include["bin_name"] = ("{cli_id}-{target}.{extension}").format(
                **variations
            )
            matrix.add_includes(bin_name_include)

        # Pass project-specific Nuitka flags from [tool.repomatic] config.
        nuitka_extra_args = " ".join(self.config["nuitka-extra-args"])
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

    @cached_property
    def release_notes(self) -> str | None:
        """Generate notes to be attached to the GitHub release.

        Renders the ``github-releases`` template with changelog
        content for the version. The template is the single place
        that defines the release body layout.
        """
        from .github.release_sync import build_expected_body

        version = self.released_version
        if not version:
            version = self.current_version
        if not version:
            return None

        changelog_path = Path("./changelog.md")
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

        Builds the same body as :attr:`release_notes`, but injects a
        ``> [!NOTE]`` admonition linking to PyPI and GitHub even before
        ``fix-changelog`` has a chance to update ``changelog.md``.  This
        lets the ``create-release`` workflow step include the admonition
        at creation time when ``publish-pypi`` succeeds.

        Returns ``None`` when the project is not on PyPI, has no
        changelog, or has no version to release.
        """
        from .changelog import (
            GITHUB_RELEASE_URL,
            PYPI_PROJECT_URL,
            build_release_admonition,
        )
        from .github.release_sync import build_expected_body

        version = self.released_version
        if not version:
            version = self.current_version
        if not version or not self.package_name:
            return None

        changelog_path = Path("./changelog.md")
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
        dialect: Dialect = Dialect.github,  # type: ignore[assignment]
    ) -> str:
        """Returns all metadata in the specified format.

        Defaults to GitHub dialect.
        """
        metadata: dict[str, Any] = {
            "is_bot": self.is_bot,
            "skip_binary_build": self.skip_binary_build,
            "new_commits": self.new_commits_hash,
            "release_commits": self.release_commits_hash,
            "mailmap_exists": self.mailmap_exists,
            "gitignore_exists": self.gitignore_exists,
            "renovate_config_exists": self.renovate_config_exists,
            "python_files": self.python_files,
            "json_files": self.json_files,
            "yaml_files": self.yaml_files,
            "toml_files": self.toml_files,
            "workflow_files": self.workflow_files,
            "doc_files": self.doc_files,
            "markdown_files": self.markdown_files,
            "image_files": self.image_files,
            "zsh_files": self.zsh_files,
            "is_python_project": self.is_python_project,
            "package_name": self.package_name,
            "project_description": self.project_description,
            "mypy_params": self.mypy_params,
            "current_version": self.current_version,
            "released_version": self.released_version,
            "is_sphinx": self.is_sphinx,
            "active_autodoc": self.active_autodoc,
            "release_notes": self.release_notes,
            "release_notes_with_admonition": self.release_notes_with_admonition,
            "new_commits_matrix": self.new_commits_matrix,
            "release_commits_matrix": self.release_commits_matrix,
            "build_targets": FLAT_BUILD_TARGETS,
            "nuitka_matrix": self.nuitka_matrix,
            "minor_bump_allowed": self.minor_bump_allowed,
            "major_bump_allowed": self.major_bump_allowed,
        }

        # Add config from [tool.repomatic] in pyproject.toml.
        # Convert kebab-case config keys to snake_case metadata keys.
        # Exclude unstable-targets (dedicated property with validation logic) and
        # subcommand config fields (read directly by test-plan and deps-graph).
        for f in fields(Config):
            if (
                f.name not in ("unstable_targets", "nuitka_extra_args")
                and f.name not in SUBCOMMAND_CONFIG_FIELDS
            ):
                config_key = f.name.replace("_", "-")
                metadata[f.name] = self.config[config_key]

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
        else:
            assert dialect == Dialect.json
            content = json.dumps(metadata, cls=JSONMetadata, indent=2)

        logging.debug(f"Formatted metadata:\n{content}")

        return content
