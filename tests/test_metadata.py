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

"""Tests for `repomatic.metadata`: the CI metadata singleton and its dump."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from click_extra.testing import CliRunner
from extra_platforms import is_windows

from repomatic.binary import NUITKA_BUILD_TARGETS
from repomatic.cli import (
    EMOJI_PRESENTATION_SELECTOR,
    JOB_COUNT_MARK,
    TEST_MATRIX_STATE_DISPLAY,
    format_matrix_cell,
    repomatic,
)
from repomatic.config import (
    Config,
)
from repomatic.github.actions import NULL_SHA
from repomatic.github.ci_status import UNSTABLE_GLYPH
from repomatic.github.matrix import (
    OS_AXIS,
    PIVOT_CELL_SEPARATOR,
    PYTHON_VERSION_AXIS,
)
from repomatic.matrix_axes import (
    PRERELEASE_LABEL_SUFFIX,
    SINGLE_RUNNER_PYTHON_VERSIONS,
    TEST_PYTHON_FULL,
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
    UNSTABLE_PYTHON_VERSIONS,
    python_version_sort_key,
)
from repomatic.metadata import (
    _METADATA_KEY_DESCRIPTIONS,
    Dialect,
    Metadata,
    _metadata_config_fields,
    all_metadata_keys,
    metadata_keys_reference,
)
from tests.conftest import metadata_from_pyproject


def regex(pattern: str) -> re.Pattern:
    """Compile a regex pattern with DOTALL flag."""
    return re.compile(pattern, re.DOTALL)


@pytest.fixture(autouse=True)
def _scrub_workflow_event(monkeypatch):
    """Detach every test from the CI run's own workflow event.

    In CI the suite inherits the real run's ``GITHUB_EVENT_NAME`` and event
    payload, which event-derived properties (``event_type``, ``commit_range``,
    ``nuitka_matrix``'s canary gating) read, so expectations would differ
    between CI and a local run. Deleting the event markers pins every test to
    the deterministic local behavior; tests exercising CI context set the
    variables back explicitly via ``monkeypatch.setenv``.
    """
    for envvar in (
        "GITHUB_BASE_REF",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_HEAD_REF",
    ):
        monkeypatch.delenv(envvar, raising=False)


class OptionalList:
    """Matcher for values that can be None or a list matching a pattern.

    Used for fields like ``new_commits`` that are ``None`` outside GitHub Actions
    but contain commit SHAs when running in CI with event data.
    """

    def __init__(self, item_pattern: re.Pattern) -> None:
        self.item_pattern = item_pattern


class OptionalString:
    """Matcher for GitHub format values that can be empty or space-separated items.

    GitHub Actions format converts None to empty string and lists to space-separated
    quoted strings. This class matches either case.
    """

    def __init__(self, item_pattern: re.Pattern) -> None:
        self.item_pattern = item_pattern


class AnyBool:
    """Matcher that accepts either True or False.

    Used for fields that depend on repository state and can vary between test runs.
    """


class AnyBoolString:
    """Matcher that accepts either 'true' or 'false' string.

    Used for GitHub format booleans that depend on repository state.
    """


class AnyLengthList:
    """Matcher for lists where each item matches a pattern, regardless of length.

    Used for matrix fields like ``commit`` where the number of commits can vary
    depending on how many are pushed together.
    """

    def __init__(self, item_pattern: re.Pattern) -> None:
        self.item_pattern = item_pattern


class StringList(list):
    """A list of plain strings serialized without double-quoting in GitHub Actions format.

    Used for metadata fields like ``cli_scripts`` that contain plain string values
    (not file paths). File path lists use double-quoted space-separated format because
    the actual metadata holds ``Path`` objects; plain string lists do not.
    """


class PartialIncludeList:
    """Matcher for include lists where required items must be present.

    The nuitka_matrix ``include`` list contains per-commit entries that multiply
    when multiple commits are pushed together. This matcher validates that all
    required items (build targets, entry point info, state) are present, while
    allowing additional commit-specific items.
    """

    def __init__(self, required_items: list[dict]) -> None:
        self.required_items = required_items


class OptionalMatrix:
    """Matcher for values that can be None or a matrix dict with commit data.

    Used for fields like ``new_commits_matrix`` that are ``None`` outside GitHub Actions
    but contain a matrix dict when running in CI with event data.
    """


class OptionalMatrixOrEmptyString:
    """Matcher for GitHub format values that can be empty string or a matrix dict.

    GitHub Actions format converts None to empty string. In CI, the value is a dict.
    """


class OptionalVersionString:
    """Matcher for values that can be None or a version string.

    Used for ``released_version`` which is ``None`` during development but contains
    a version string like ``"5.5.0"`` on release branches.
    """

    def __init__(self, version_pattern: re.Pattern) -> None:
        self.version_pattern = version_pattern


class OptionalVersionOrEmptyString:
    """Matcher for GitHub format values that can be empty string or a version string.

    GitHub Actions format converts None to empty string. On release branches,
    the value is a version string.
    """

    def __init__(self, version_pattern: re.Pattern) -> None:
        self.version_pattern = version_pattern


class AnyReleaseNotes:
    """Matcher for release notes that vary based on development vs release state.

    During development, release notes contain a warning that the version is not
    released yet. On release branches, the notes contain actual release content.
    None is valid when the changelog section is empty (e.g. right after a release).
    """

    def __init__(self, dev_pattern: re.Pattern, release_pattern: re.Pattern) -> None:
        self.dev_pattern = dev_pattern
        self.release_pattern = release_pattern


class AnyReleaseNotesOrEmptyString:
    """Matcher for GitHub format release notes.

    Can be empty string (when no version), None (empty changelog section),
    or match either development or release notes patterns.
    """

    def __init__(self, dev_pattern: re.Pattern, release_pattern: re.Pattern) -> None:
        self.dev_pattern = dev_pattern
        self.release_pattern = release_pattern


def _matches_pattern(item: Any, pattern: dict) -> bool:
    """Check if an item matches a pattern dict.

    Returns True if item is a dict with all keys from pattern, and each value
    matches (either exact match or regex match for Pattern objects).
    """
    if not isinstance(item, dict):
        return False
    if not set(pattern.keys()).issubset(set(item.keys())):
        return False
    for key, expected_value in pattern.items():
        actual_value = item[key]
        if isinstance(expected_value, re.Pattern):
            if not isinstance(actual_value, str):
                return False
            if re.fullmatch(expected_value, actual_value) is None:
                return False
        elif actual_value != expected_value:
            return False
    return True


def iter_checks(metadata: Any, expected: Any, context: Any) -> None:
    """Recursively iterate over expected content and check it matches in metadata."""

    if isinstance(expected, re.Pattern):
        assert isinstance(metadata, str)
        assert re.fullmatch(expected, metadata) is not None, (
            f"{metadata!r} does not match {expected.pattern!r} in {context!r}"
        )

    elif isinstance(expected, OptionalList):
        # Allow None or a list of items matching the pattern.
        if metadata is None:
            return
        assert isinstance(metadata, list), (
            f"{metadata!r} should be None or a list in {context!r}"
        )
        for item in metadata:
            assert isinstance(item, str), f"{item!r} should be a string in {context!r}"
            assert re.fullmatch(expected.item_pattern, item) is not None, (
                f"{item!r} does not match {expected.item_pattern.pattern!r} in {context!r}"
            )

    elif isinstance(expected, OptionalString):
        # Allow None, empty string, or space-separated items matching the pattern.
        # None occurs in github_json format (JSON null); empty string in github format.
        if metadata is None:
            return
        assert isinstance(metadata, str), (
            f"{metadata!r} should be a string in {context!r}"
        )
        if metadata == "":
            return
        # Parse space-separated items: "sha1" "sha2" -> ["sha1", "sha2"].
        for item in metadata.split():
            assert re.fullmatch(expected.item_pattern, item) is not None, (
                f"{item!r} does not match {expected.item_pattern.pattern!r} in {context!r}"
            )

    elif isinstance(expected, AnyBool):
        # Accept either True or False.
        assert isinstance(metadata, bool), (
            f"{metadata!r} should be a boolean in {context!r}"
        )

    elif isinstance(expected, AnyBoolString):
        # Accept either 'true' or 'false' string.
        assert metadata in ("true", "false"), (
            f"{metadata!r} should be 'true' or 'false' in {context!r}"
        )

    elif isinstance(expected, AnyLengthList):
        # Accept a list of any length where each item matches the pattern.
        assert isinstance(metadata, list), (
            f"{metadata!r} should be a list in {context!r}"
        )
        assert len(metadata) >= 1, f"list should have at least one item in {context!r}"
        for item in metadata:
            assert isinstance(item, str), f"{item!r} should be a string in {context!r}"
            assert re.fullmatch(expected.item_pattern, item) is not None, (
                f"{item!r} does not match {expected.item_pattern.pattern!r} in {context!r}"
            )

    elif isinstance(expected, PartialIncludeList):
        # Accept a list where all required items are present, allowing extras.
        assert isinstance(metadata, list), (
            f"{metadata!r} should be a list in {context!r}"
        )
        assert len(metadata) >= len(expected.required_items), (
            f"list should have at least {len(expected.required_items)} items in "
            f"{context!r}"
        )
        # Check each required item has at least one match in metadata.
        for required in expected.required_items:
            found = False
            for item in metadata:
                if _matches_pattern(item, required):
                    found = True
                    break
            assert found, (
                f"required item {required!r} not found in metadata list in {context!r}"
            )

    elif isinstance(expected, OptionalMatrix):
        # Allow None or a matrix dict with commit data.
        if metadata is None:
            return
        assert isinstance(metadata, dict), (
            f"{metadata!r} should be None or a dict in {context!r}"
        )
        # Validate basic matrix structure if present.
        if "commit" in metadata:
            assert isinstance(metadata["commit"], list)

    elif isinstance(expected, OptionalMatrixOrEmptyString):
        # Allow empty string or a matrix dict.
        if metadata == "":
            return
        assert isinstance(metadata, dict), (
            f"{metadata!r} should be '' or a dict in {context!r}"
        )
        # Validate basic matrix structure if present.
        if "commit" in metadata:
            assert isinstance(metadata["commit"], list)

    elif isinstance(expected, OptionalVersionString):
        # Allow None or a version string matching the pattern.
        if metadata is None:
            return
        assert isinstance(metadata, str), (
            f"{metadata!r} should be None or a string in {context!r}"
        )
        assert re.fullmatch(expected.version_pattern, metadata) is not None, (
            f"{metadata!r} does not match {expected.version_pattern.pattern!r} in {context!r}"
        )

    elif isinstance(expected, OptionalVersionOrEmptyString):
        # Allow empty string or a version string matching the pattern.
        if metadata == "":
            return
        assert isinstance(metadata, str), (
            f"{metadata!r} should be '' or a string in {context!r}"
        )
        assert re.fullmatch(expected.version_pattern, metadata) is not None, (
            f"{metadata!r} does not match {expected.version_pattern.pattern!r} in {context!r}"
        )

    elif isinstance(expected, AnyReleaseNotes):
        # Allow None (empty changelog section) or release notes matching either
        # development or release pattern.
        if metadata is None:
            return
        assert isinstance(metadata, str), (
            f"{metadata!r} should be a string in {context!r}"
        )
        dev_match = re.fullmatch(expected.dev_pattern, metadata) is not None
        release_match = re.fullmatch(expected.release_pattern, metadata) is not None
        assert dev_match or release_match, (
            f"{metadata!r} does not match dev pattern {expected.dev_pattern.pattern!r} "
            f"or release pattern {expected.release_pattern.pattern!r} in {context!r}"
        )

    elif isinstance(expected, AnyReleaseNotesOrEmptyString):
        # Allow empty string, None (empty changelog section), or release notes
        # matching either pattern.
        if metadata is None or metadata == "":
            return
        assert isinstance(metadata, str), (
            f"{metadata!r} should be '' or a string in {context!r}"
        )
        dev_match = re.fullmatch(expected.dev_pattern, metadata) is not None
        release_match = re.fullmatch(expected.release_pattern, metadata) is not None
        assert dev_match or release_match, (
            f"{metadata!r} does not match dev pattern {expected.dev_pattern.pattern!r} "
            f"or release pattern {expected.release_pattern.pattern!r} in {context!r}"
        )

    elif isinstance(expected, dict):
        assert isinstance(metadata, dict)
        assert set(metadata) == set(expected)
        for key, value in expected.items():
            # By convention, keys ending with "_files" are path strings so they need to
            # be adjusted for Windows.
            if key.endswith("_files") and is_windows():
                # Path are stored as a list in JSON format.
                # Re-sort with case-insensitive key to match Windows Path ordering.
                if isinstance(value, list):
                    value = sorted(
                        (v.replace("/", "\\") for v in value),
                        key=str.casefold,
                    )
                # Path are space-separated quoted strings in GitHub format.
                # Re-sort to match Windows case-insensitive Path ordering.
                elif value:
                    paths = [p.replace("/", "\\") for p in value.split('" "')]
                    # Strip outer quotes from first/last, sort, re-quote.
                    paths[0] = paths[0].lstrip('"')
                    paths[-1] = paths[-1].rstrip('"')
                    paths.sort(key=str.casefold)
                    value = " ".join(f'"{p}"' for p in paths)
                else:
                    value = value.replace("/", "\\")

            iter_checks(metadata[key], value, metadata)

    elif isinstance(expected, list):
        assert isinstance(metadata, list), (
            f"{metadata!r} should be a list in {context!r}"
        )
        if len(metadata) != len(expected):
            # The file inventories are the long lists here, and a bare length
            # comparison reduces a tree that drifted from the git index to a
            # pair of integers, leaving the reader to hunt the paths down in
            # another test's output. Name the difference instead: the
            # `_ALL_TRACKED` docstring makes enumerating drift this test's job.
            missing = [item for item in expected if item not in metadata]
            unexpected = [item for item in metadata if item not in expected]
            raise AssertionError(
                f"Got {len(metadata)} items, expected {len(expected)}. "
                f"Missing: {missing!r}. Unexpected: {unexpected!r}."
            )
        for produced, wanted in zip(metadata, expected, strict=True):
            iter_checks(produced, wanted, metadata)

    else:
        assert metadata == expected, (
            f"{metadata!r} does not match {expected!r} in {context!r}"
        )
        assert type(metadata) is type(expected)


_ALL_TRACKED = tuple(
    subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=True,
    ).stdout.splitlines()
)
"""Every path in the git index, snapshotted once at collection time.

The independent oracle behind the file-inventory expectations below:
`Metadata.glob_files` walks the disk under gitignore filtering, while this
reads the index, so their agreement proves the two views of the tree match. A
stray untracked file (or a tracked file missing on disk) still fails the
comparison, deliberately: the metadata a workflow consumes must describe the
committed tree, and enumerating drift by name is the test's job.
"""


def _tracked_inventory(
    *extensions: str,
    subdir: str = "",
    names: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> list[str]:
    """Filter {data}`_ALL_TRACKED` the way one `glob_files` pattern would.

    *extensions* mirror a brace set (`**/*.{py,pyi}`), *names* match exact
    basenames (`**/pyproject.toml`), *subdir* prefixes the walk
    (`.github/workflows/**`), and *exclude* mirrors a `!**/{name}` negation.

    Symlinked twins collapse onto their target's spelling exactly like
    `glob_files`, which resolves every path and drops duplicates: the
    `repomatic/data/` symlinks to workflows, agents and skills never surface
    under their link names.
    """
    repo_root = Path.cwd()
    seen = set()
    for line in _ALL_TRACKED:
        if subdir and not line.startswith(subdir):
            continue
        basename = line.rsplit("/", 1)[-1]
        if basename in exclude:
            continue
        suffix = basename.rsplit(".", 1)[-1] if "." in basename else ""
        if not (suffix in extensions or basename in names):
            continue
        resolved = (repo_root / line).resolve()
        try:
            seen.add(resolved.relative_to(repo_root).as_posix())
        except ValueError:
            seen.add(line)
    return sorted(seen)


MARKDOWN_EXTENSIONS = (
    "markdown",
    "mdown",
    "mkdn",
    "mdwn",
    "mkd",
    "md",
    "mdtxt",
    "mdtext",
    "mdx",
)
"""The `markdown_files` brace set, mirroring `Metadata.markdown_files`."""

MARKDOWN_INVENTORY = _tracked_inventory(*MARKDOWN_EXTENSIONS)
"""Every Markdown file tracked in the repository, in `glob_files` order.

Backs both `doc_files` and `markdown_files`. The two metadata keys glob
different extension sets: `doc_files` also takes `.rst` and `.tex`. They
coincide only because the repository currently ships neither, so the day one
lands the two expectations below have to diverge.
"""


expected: dict[str, Any] = {
    "is_bot": AnyBool(),
    # skip_binary_build depends on the event type and changed files. In CI push events
    # where only non-binary-affecting files changed, it is True.
    "skip_binary_build": AnyBool(),
    # *_changed booleans depend on the current event's commit range.
    "yaml_changed": AnyBool(),
    "zsh_changed": AnyBool(),
    "workflows_changed": AnyBool(),
    # new_commits is None when running outside GitHub Actions (no event data).
    # In CI, it contains commit SHAs extracted from the push event payload.
    "new_commits": OptionalList(regex(r"[a-f0-9]{40}")),
    # release_commits is None when there are no release commits in the event.
    # It contains SHAs only when a "[changelog] Release vX.Y.Z" commit is present.
    "release_commits": OptionalList(regex(r"[a-f0-9]{40}")),
    "mailmap_exists": True,
    "gitignore_exists": True,
    "python_files": _tracked_inventory("py", "pyi", "pyw", "pyx", "ipynb"),
    "json_files": _tracked_inventory(
        "json",
        "jsonc",
        names=(".code-workspace",),
        exclude=("package-lock.json",),
    ),
    "yaml_files": _tracked_inventory("yaml", "yml"),
    "pyproject_files": _tracked_inventory(names=("pyproject.toml",)),
    "workflow_files": _tracked_inventory("yaml", "yml", subdir=".github/workflows/"),
    "doc_files": MARKDOWN_INVENTORY,
    "markdown_files": MARKDOWN_INVENTORY,
    "image_files": _tracked_inventory("jpeg", "jpg", "png", "webp", "avif"),
    "shfmt_files": [".claude/package-skills.sh"],
    # Empty: the repository's only shell script is bash, and a `.sh` file
    # joins zsh_files only when its shebang names zsh.
    "zsh_files": [],
    "is_python_project": True,
    "is_python_package": True,
    "binaries_sync": True,
    "manpages_script": "repomatic.cli:repomatic",
    "manpages_asset_name": "",
    # Plain strings, not Path objects, so GitHub format joins them unquoted.
    "release_assets": StringList(["repomatic-claude-plugin.zip"]),
    "nuitka_extras": [],
    "package_name": "repomatic",
    "cli_scripts": StringList(["repomatic"]),
    "project_description": "🏭 Automate repository maintenance, releases, and CI/CD workflows",
    "mypy_params": StringList(["--python-version", "3.10"]),
    "current_version": regex(r"[0-9\.]+(\.dev[0-9]+)?"),
    # released_version is None during development, but contains a version string
    # on release branches (e.g., "5.5.0" when a "[changelog] Release v5.5.0"
    # commit exists).
    "released_version": OptionalVersionString(regex(r"[0-9]+\.[0-9]+\.[0-9]+")),
    "is_sphinx": True,
    "active_autodoc": True,
    "uses_myst": True,
    # Release notes are verbatim changelog content.
    # Development: starts with the unreleased warning admonition.
    # Release: contains actual changelog entries (bullets, admonitions).
    "release_notes": AnyReleaseNotes(
        dev_pattern=regex(
            r"> \[\!WARNING\]\n"
            r"> This version is \*\*not released yet\*\* and is under active development\.\n\n"
            r".+"
        ),
        release_pattern=regex(
            r"(?:(?!> \[\!WARNING\]).+|"  # With changelog entries.
            # With admonition (e.g. NOTE or CAUTION).
            r"> \[\!(NOTE|CAUTION)\]\n>.+)"
        ),
    ),
    # Same as release_notes, but always includes a pre-computed availability
    # admonition with PyPI and GitHub links.
    "release_notes_with_admonition": AnyReleaseNotes(
        dev_pattern=regex(
            r"> \[\!WARNING\]\n"
            r"> This version is \*\*not released yet\*\* and is under active development\.\n\n"
            r"> \[\!NOTE\]\n"
            r"> .+is available on.+\n\n"
            r".+"
        ),
        release_pattern=regex(
            r"> \[\!NOTE\]\n"
            r"> .+is available on.+"
        ),
    ),
    # new_commits_matrix is None when running outside GitHub Actions.
    # In CI, it contains a matrix dict with commit data.
    "new_commits_matrix": OptionalMatrix(),
    # release_commits_matrix is None when there are no release commits.
    # It contains a matrix dict only when a "[changelog] Release vX.Y.Z"
    # commit is present.
    "release_commits_matrix": OptionalMatrix(),
    "build_targets": [
        {
            "target": "linux-arm64",
            "os": "ubuntu-26.04-arm",
            "platform_id": "linux",
            "arch": "arm64",
            "extension": "bin",
            "container": (
                "quay.io/pypa/manylinux_2_28_aarch64@sha256:"
                "e7035406e58d96b7407246af1f6514a3cbd753a0025b42b9adfbeadd3b29ba80"
            ),
            "glibc_floor": "2.28",
        },
        {
            "target": "linux-x64",
            "os": "ubuntu-26.04",
            "platform_id": "linux",
            "arch": "x64",
            "extension": "bin",
            "container": (
                "quay.io/pypa/manylinux_2_28_x86_64@sha256:"
                "fdb9a9c223b215604dc7b6f7e8fff4b39bfea5fbaa7777a2e5544a60dfa437f8"
            ),
            "glibc_floor": "2.28",
        },
        {
            "target": "macos-arm64",
            "os": "macos-26",
            "platform_id": "macos",
            "arch": "arm64",
            "extension": "bin",
            "min_os": "11.0",
        },
        {
            "target": "macos-x64",
            "os": "macos-26-intel",
            "platform_id": "macos",
            "arch": "x64",
            "extension": "bin",
            "min_os": "10.15",
        },
        {
            "target": "windows-arm64",
            "os": "windows-11-arm",
            "platform_id": "windows",
            "arch": "arm64",
            "extension": "exe",
            "min_os": "11",
        },
        {
            "target": "windows-x64",
            "os": "windows-2025",
            "platform_id": "windows",
            "arch": "x64",
            "extension": "exe",
            "min_os": "10",
        },
    ],
    "nuitka_matrix": {
        "os": [
            "ubuntu-26.04-arm",
            "ubuntu-26.04",
            "macos-26",
            "macos-26-intel",
            "windows-11-arm",
            "windows-2025",
        ],
        "entry_point": ["repomatic"],
        "commit": AnyLengthList(regex(r"[a-z0-9]+")),
        # The include list contains per-commit entries that multiply with more commits.
        # We validate that required fixed items are present (build targets, entry point,
        # state) plus at least one commit info and one bin_name entry.
        "include": PartialIncludeList([
            # Build targets (fixed, one per platform).
            {
                "target": "linux-arm64",
                "os": "ubuntu-26.04-arm",
                "platform_id": "linux",
                "arch": "arm64",
                "extension": "bin",
                "container": (
                    "quay.io/pypa/manylinux_2_28_aarch64@sha256:"
                    "e7035406e58d96b7407246af1f6514a3cbd753a0025b42b9adfbeadd3b29ba80"
                ),
                "glibc_floor": "2.28",
            },
            {
                "target": "linux-x64",
                "os": "ubuntu-26.04",
                "platform_id": "linux",
                "arch": "x64",
                "extension": "bin",
                "container": (
                    "quay.io/pypa/manylinux_2_28_x86_64@sha256:"
                    "fdb9a9c223b215604dc7b6f7e8fff4b39bfea5fbaa7777a2e5544a60dfa437f8"
                ),
                "glibc_floor": "2.28",
            },
            {
                "target": "macos-arm64",
                "os": "macos-26",
                "platform_id": "macos",
                "arch": "arm64",
                "extension": "bin",
                "min_os": "11.0",
            },
            {
                "target": "macos-x64",
                "os": "macos-26-intel",
                "platform_id": "macos",
                "arch": "x64",
                "extension": "bin",
                "min_os": "10.15",
            },
            {
                "target": "windows-arm64",
                "os": "windows-11-arm",
                "platform_id": "windows",
                "arch": "arm64",
                "extension": "exe",
                "min_os": "11",
            },
            {
                "target": "windows-x64",
                "os": "windows-2025",
                "platform_id": "windows",
                "arch": "x64",
                "extension": "exe",
                "min_os": "10",
            },
            # Entry point info (fixed, one per entry point). The --python-flag=-m
            # workaround for __main__.py packages rides here, per entry point;
            # [tool.nuitka] is resolved at build time by `repomatic run nuitka`.
            {
                "entry_point": "repomatic",
                "cli_id": "repomatic",
                "module_id": "repomatic.__main__",
                "callable_id": "main",
                "module_path": regex(r"repomatic(/|\\)?"),
                "nuitka_python_flags": "--python-flag=-m",
            },
            # State (fixed).
            {"state": "stable"},
            # At least one commit info entry (varies by commit count).
            {
                "commit": regex(r"[a-z0-9]+"),
                "short_sha": regex(r"[a-z0-9]+"),
                "current_version": regex(r"[0-9\.]+(\.dev[0-9]+)?"),
            },
            # At least one bin_name entry per OS (varies by commit count).
            {
                "os": "ubuntu-26.04-arm",
                "entry_point": "repomatic",
                "commit": regex(r"[a-z0-9]+"),
                "bin_name": regex(r"repomatic-[\d.]+(\.dev\d+)?-linux-arm64\.bin"),
            },
            {
                "os": "ubuntu-26.04",
                "entry_point": "repomatic",
                "commit": regex(r"[a-z0-9]+"),
                "bin_name": regex(r"repomatic-[\d.]+(\.dev\d+)?-linux-x64\.bin"),
            },
            {
                "os": "macos-26",
                "entry_point": "repomatic",
                "commit": regex(r"[a-z0-9]+"),
                "bin_name": regex(r"repomatic-[\d.]+(\.dev\d+)?-macos-arm64\.bin"),
            },
            {
                "os": "macos-26-intel",
                "entry_point": "repomatic",
                "commit": regex(r"[a-z0-9]+"),
                "bin_name": regex(r"repomatic-[\d.]+(\.dev\d+)?-macos-x64\.bin"),
            },
            {
                "os": "windows-11-arm",
                "entry_point": "repomatic",
                "commit": regex(r"[a-z0-9]+"),
                "bin_name": regex(r"repomatic-[\d.]+(\.dev\d+)?-windows-arm64\.exe"),
            },
            {
                "os": "windows-2025",
                "entry_point": "repomatic",
                "commit": regex(r"[a-z0-9]+"),
                "bin_name": regex(r"repomatic-[\d.]+(\.dev\d+)?-windows-x64\.exe"),
            },
        ]),
    },
    "test_matrix": {
        "os": [
            "ubuntu-26.04-arm",
            "ubuntu-26.04",
            "macos-26",
            "macos-26-intel",
            "windows-11-arm",
            "windows-2025",
        ],
        "python-version": [
            "3.10",
            "3.14",
            "3.15",
        ],
        "include": [
            {"state": "stable"},
            {
                "state": "unstable",
                "python-version": "3.15",
                "python-label": "3.15-dev",
            },
            {"os": "ubuntu-26.04-arm", "python-version": "3.14t", "state": "stable"},
        ],
        "exclude": [
            {"os": "windows-11-arm", "python-version": "3.10"},
        ],
    },
    "test_matrix_pr": {
        "os": [
            "ubuntu-26.04-arm",
            "macos-26",
            "windows-2025",
        ],
        "python-version": [
            "3.10",
            "3.14",
        ],
        "include": [
            {"state": "stable"},
        ],
    },
    # Bump allowed values depend on comparing current version vs latest git tag.
    # These can be True or False depending on the current development cycle state.
    "minor_bump_allowed": AnyBool(),
    "major_bump_allowed": AnyBool(),
    # `minimum-release-age` default "1 week", rendered as whole days for npm.
    "npm_min_release_age_days": 7,
    # This repository publishes `.html` pages, the `sphinx.builder` default.
    "sphinx_builder": "html",
    # This repository dogfoods its own Cloudflare Pages lane on repomatic.net,
    # so it declares that target rather than the `github-pages` default, into a
    # project named after the repository (the empty override).
    "site_cloudflare_project": "",
    "site_deploy": "cloudflare-pages",
}


def test_dump_factories_match_key_descriptions():
    """The three views of the key inventory must name the same keys.

    `_METADATA_KEY_DESCRIPTIONS` (what `--list-keys` and the docs table show),
    `Metadata.dump_factories` (what the command emits) and
    `all_metadata_keys` (what the command accepts as an argument) are three
    hand-maintained lists sitting far apart in the module. A description
    without a factory makes `repomatic metadata <key>` accept the key and then
    emit nothing; a factory without a description drops the key from
    `--list-keys` and the rendered docs.
    """
    factories = set(Metadata().dump_factories())
    config_keys = set(_metadata_config_fields())

    assert set(_METADATA_KEY_DESCRIPTIONS) == factories - config_keys
    assert all_metadata_keys() == factories


def test_metadata_keys_reference_documents_every_key():
    """Every emitted key needs a non-empty description in the reference table."""
    rows = dict(metadata_keys_reference())
    assert set(rows) == all_metadata_keys()

    undocumented = sorted(key for key, text in rows.items() if not text.strip())
    assert not undocumented, (
        f"Metadata keys with no description: {undocumented}. Add one to "
        "_METADATA_KEY_DESCRIPTIONS, or an attribute docstring to the Config "
        "field."
    )


@pytest.mark.parametrize("field_name", sorted(_metadata_config_fields()))
def test_config_defaults_render_as_github_values(field_name):
    """Every config field exposed as metadata must survive GitHub encoding.

    A nested config dataclass has no GitHub encoding, so adding one without
    listing it in `SUBCOMMAND_CONFIG_FIELDS` makes `repomatic metadata` raise
    `NotImplementedError` on every run. Checking the dataclass defaults rather
    than this repository's own values keeps the guard on the declared surface.
    """
    value = getattr(Config(), field_name)
    assert isinstance(Metadata.format_github_value(value), str)


def test_metadata_github_json_format():
    raw = Metadata().dump(Dialect.github_json)
    assert isinstance(raw, str)

    # Output must be a single line starting with "metadata=".
    lines = raw.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("metadata=")

    json_str = lines[0][len("metadata=") :]
    metadata = json.loads(json_str)

    # In github_json format, list/tuple values are pre-formatted via
    # format_github_value() because GitHub Actions stringifies JSON arrays
    # as "Array" when interpolated in ${{ }} expressions. Transform expected
    # values to match: file lists become space-separated quoted strings,
    # plain string lists become space-separated unquoted strings, and
    # dict lists become JSON strings.
    github_json_expected = dict(expected)
    for key, value in github_json_expected.items():
        if isinstance(value, OptionalList):
            # Convert OptionalList to OptionalString for github_json format.
            github_json_expected[key] = OptionalString(value.item_pattern)
        elif isinstance(value, list):
            if not value:
                github_json_expected[key] = ""
            elif all(isinstance(i, str) for i in value):
                # StringList items are unquoted; file list items are double-quoted.
                if key.endswith("_files"):
                    github_json_expected[key] = " ".join(f'"{v}"' for v in value)
                else:
                    github_json_expected[key] = " ".join(value)
            elif all(isinstance(i, dict) for i in value):
                github_json_expected[key] = json.dumps(value)

    iter_checks(metadata, github_json_expected, raw)


def test_metadata_github_json_format_key_filtering():
    raw = Metadata().dump(
        Dialect.github_json,
        keys=("is_python_project", "current_version"),
    )
    json_str = raw.strip().removeprefix("metadata=")
    metadata = json.loads(json_str)

    assert set(metadata.keys()) == {"is_python_project", "current_version"}


def test_file_inventories_are_not_vacuous():
    """The git-derived inventories really enumerate the tree.

    Guards the `_tracked_inventory` derivations against a filter typo
    silently emptying a list: each inventory must contain a signature file
    that will exist for the life of the project, and the symlink collapse
    must keep targets, never link names.
    """
    assert "repomatic/cli.py" in expected["python_files"]
    assert "tests/test_metadata.py" in expected["python_files"]
    assert ".github/workflows/tests.yaml" in expected["workflow_files"]
    assert ".github/workflows/autofix.yaml" in expected["yaml_files"]
    assert expected["pyproject_files"] == ["pyproject.toml"]
    assert ".claude-plugin/plugin.json" in expected["json_files"]
    assert "changelog.md" in MARKDOWN_INVENTORY
    assert "readme.md" in MARKDOWN_INVENTORY
    assert "docs/assets/icon.png" in expected["image_files"]
    # Symlink twins collapse onto their targets, like `glob_files` resolves.
    assert "repomatic/data/autofix.yaml" not in expected["yaml_files"]
    assert "repomatic/data/agent-grunt-qa.md" not in MARKDOWN_INVENTORY


def test_metadata_json_format():
    metadata = Metadata().dump(Dialect.json)
    assert isinstance(metadata, str)

    iter_checks(json.loads(metadata), expected, metadata)


def test_metadata_github_format():
    raw_metadata = Metadata().dump()
    assert isinstance(raw_metadata, str)

    # Prepare metadata for checks
    metadata = {}
    # Accumulation states.
    acc_key = None
    acc_delimiter = None
    acc_lines = []
    for line in raw_metadata.splitlines():
        # We are at the end of the accumulation for a key.
        if line == acc_delimiter:
            assert acc_delimiter
            assert acc_key
            assert acc_lines
            metadata[acc_key] = "\n".join(acc_lines)
            # Reset accumulation states.
            acc_key = None
            acc_delimiter = None
            acc_lines = []
            continue

        # We are accumulating lines for a key.
        if acc_key:
            acc_lines.append(line)
            continue

        # We should not have any accumulation state at this point.
        assert acc_key is None
        assert acc_delimiter is None
        assert acc_lines == []

        # We are starting a new accumulation for a key.
        if "<<" in line:
            # Check the delimiter syntax.
            assert line.count("<<") == 1
            acc_key, acc_delimiter = line.split("<<", 1)
            assert re.fullmatch(r"GHA_DELIMITER_[0-9]+", acc_delimiter)
            continue

        # We are at a simple key-value pair.
        if "=" in line:
            key, value = line.split("=", 1)
            # Convert list-like and dict-like JSON string into Python objects.
            if value.startswith(("[", "{")):
                value = json.loads(value)
            metadata[key] = value
            continue

        raise ValueError(
            f"Unexpected line format in metadata: {line!r}. "
            "Expecting a key-value pair or a delimited block."
        )

    # Adapt expected values to match GitHub Actions format.
    github_format_expected = {}
    for key, value in expected.items():
        new_value = value
        if value is None:
            new_value = ""
        elif isinstance(value, bool):
            new_value = str(value).lower()
        elif isinstance(value, int):
            new_value = str(value)
        elif isinstance(value, OptionalList):
            # Convert OptionalList to OptionalString for GitHub format.
            new_value = OptionalString(value.item_pattern)
        elif isinstance(value, AnyBool):
            # Convert AnyBool to AnyBoolString for GitHub format.
            new_value = AnyBoolString()
        elif isinstance(value, OptionalMatrix):
            # Convert OptionalMatrix to OptionalMatrixOrEmptyString for GitHub format.
            new_value = OptionalMatrixOrEmptyString()
        elif isinstance(value, OptionalVersionString):
            # Convert OptionalVersionString to OptionalVersionOrEmptyString for GitHub
            # format.
            new_value = OptionalVersionOrEmptyString(value.version_pattern)
        elif isinstance(value, AnyReleaseNotes):
            # Convert AnyReleaseNotes to AnyReleaseNotesOrEmptyString for GitHub format.
            new_value = AnyReleaseNotesOrEmptyString(
                value.dev_pattern, value.release_pattern
            )
        elif isinstance(value, StringList):
            # Plain string lists: space-separated without double-quotes.
            new_value = " ".join(value)
        elif isinstance(value, list) and all(isinstance(i, str) for i in value):
            # File path lists (Path objects in actual metadata): double-quoted.
            new_value = " ".join(f'"{i}"' for i in value)
        github_format_expected[key] = new_value

    iter_checks(metadata, github_format_expected, raw_metadata)


def test_metadata_command_renders_under_captured_runner():
    """`repomatic metadata` renders to a captured stdout that has no descriptor.

    The docs `{click:run}` directive live-renders this command through click-extra's
    in-memory runner, which is Click's default `capture="sys"` mode: its stdout has
    no `fileno()`. `prep_path` must degrade to that stream rather than reopening its
    descriptor, and a bare stdout run must stay silent (no spurious overwrite
    warning) so the rendered block is pure JSON. See `repomatic.cli.prep_path`.
    """
    result = CliRunner().invoke(
        repomatic, ["metadata", "test_matrix", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    # A `prep_path` crash (exit 1) or a leaked warning line both break this parse.
    data = json.loads(result.output)
    assert "test_matrix" in data


@pytest.mark.parametrize(
    ("versions", "expected"),
    [
        (["3.15", "3.10"], ["3.10", "3.15"]),
        (["3.14t", "3.14"], ["3.14", "3.14t"]),
        (["3.15", "3.14t", "3.10", "3.14"], ["3.10", "3.14", "3.14t", "3.15"]),
        (["3.10.2", "3.10"], ["3.10", "3.10.2"]),
    ],
)
def test_python_version_sort_key_orders_by_release(versions, expected):
    """Versions sort numerically, a flavor right after its base version."""
    assert sorted(versions, key=python_version_sort_key) == expected


def test_show_test_matrix_renders_full_grid():
    """`repomatic show-test-matrix` renders the full matrix as a labelled grid."""
    result = CliRunner().invoke(repomatic, ["show-test-matrix"])
    assert result.exit_code == 0, result.output
    assert "Python" in result.output
    assert "ubuntu-26.04-arm" in result.output
    # The matrix always carries stable jobs, decorated with an emoji by default.
    assert "✅ stable" in result.output


def test_show_test_matrix_rows_sorted_by_release():
    """Grid rows order Python versions by release, flavors after their base."""
    result = CliRunner().invoke(
        repomatic, ["--table-format", "json", "show-test-matrix", "full"]
    )
    assert result.exit_code == 0, result.output
    versions = [row["Python"] for row in json.loads(result.output)]
    assert versions == sorted(versions, key=python_version_sort_key)


@pytest.mark.parametrize(
    ("matrix_name", "canonical"),
    [("full", TEST_RUNNERS_FULL), ("pr", TEST_RUNNERS_PR)],
)
def test_show_test_matrix_columns_follow_canonical_order(matrix_name, canonical):
    """OS columns follow the runner order of the axis constants."""
    result = CliRunner().invoke(
        repomatic, ["--table-format", "json", "show-test-matrix", matrix_name]
    )
    assert result.exit_code == 0, result.output
    columns = [key for key in json.loads(result.output)[0] if key != "Python"]
    # Canonical runners keep their declared order; any runner the config added
    # trails them in first-seen order.
    expected = [runner for runner in canonical if runner in columns]
    expected += [col for col in columns if col not in canonical]
    assert columns == expected


def test_show_test_matrix_pr_is_reduced():
    """The `pr` argument selects a grid with fewer rows and columns than `full`."""
    full = CliRunner().invoke(
        repomatic, ["--table-format", "json", "show-test-matrix", "full"]
    )
    pr = CliRunner().invoke(
        repomatic, ["--table-format", "json", "show-test-matrix", "pr"]
    )
    assert full.exit_code == 0, full.output
    assert pr.exit_code == 0, pr.output
    full_rows = json.loads(full.output)
    pr_rows = json.loads(pr.output)
    # Fewer Python rows, and fewer columns per row (one key per OS, plus Python).
    assert len(pr_rows) < len(full_rows)
    assert len(pr_rows[0]) < len(full_rows[0])


def test_show_test_matrix_no_emoji_uses_plain_words():
    """`--no-emoji` renders bare state words, deriving the expectation from config."""
    plain = CliRunner().invoke(
        repomatic, ["--table-format", "json", "show-test-matrix", "--no-emoji"]
    )
    fancy = CliRunner().invoke(repomatic, ["show-test-matrix"])
    assert plain.exit_code == 0, plain.output
    assert fancy.exit_code == 0, fancy.output
    for state, label in TEST_MATRIX_STATE_DISPLAY.items():
        assert label.removesuffix(f" {state}") not in plain.output
    states = {
        value.split(f" {JOB_COUNT_MARK}")[0]
        for row in json.loads(plain.output)
        for key, value in row.items()
        if key != "Python"
    }
    # Each plain state present gains its glyph prefix in the default rendering.
    for state in states & set(TEST_MATRIX_STATE_DISPLAY):
        assert TEST_MATRIX_STATE_DISPLAY[state] in fancy.output


@pytest.mark.parametrize(("state", "label"), tuple(TEST_MATRIX_STATE_DISPLAY.items()))
def test_show_test_matrix_labels_drop_the_emoji_selector(state, label):
    """No grid label carries the selector terminals and `wcwidth` disagree on.

    The glyph a job name carries does, which is what makes the labels a
    separate question: a grid padded for two columns and drawn in one breaks
    every rule to the right of that cell.
    """
    assert EMOJI_PRESENTATION_SELECTOR in UNSTABLE_GLYPH
    assert EMOJI_PRESENTATION_SELECTOR not in label, f"{state}: {label!r}"


def test_format_matrix_cell_labels_every_state():
    """Each state of a cell shared by several jobs gets its own glyph."""
    tally = Counter(dict.fromkeys(TEST_MATRIX_STATE_DISPLAY, 1))
    assert format_matrix_cell("", tally) == PIVOT_CELL_SEPARATOR.join(
        TEST_MATRIX_STATE_DISPLAY.values()
    )


@pytest.mark.parametrize(("state", "label"), tuple(TEST_MATRIX_STATE_DISPLAY.items()))
def test_format_matrix_cell_counts_jobs_past_the_first(state, label):
    """A state carrying several jobs says how many; a lone job stays bare."""
    assert format_matrix_cell("", Counter({state: 1})) == label
    assert format_matrix_cell("", Counter({state: 3})) == f"{label} {JOB_COUNT_MARK}3"
    # A count is data, not decoration: --no-emoji keeps it.
    assert (
        format_matrix_cell("", Counter({state: 3}), emoji=False)
        == f"{state} {JOB_COUNT_MARK}3"
    )


def test_format_matrix_cell_keeps_the_empty_intersection_placeholder():
    """A cell no job occupies renders whatever `pivot` put there."""
    assert format_matrix_cell("—", None) == "—"


def test_show_test_matrix_pivots_on_the_chosen_axes():
    """--row-axis and --col-axis transpose the grid onto the named job keys."""
    default = CliRunner().invoke(
        repomatic, ["--table-format", "json", "show-test-matrix"]
    )
    transposed = CliRunner().invoke(
        repomatic,
        [
            "--table-format",
            "json",
            "show-test-matrix",
            "--row-axis",
            OS_AXIS,
            "--col-axis",
            PYTHON_VERSION_AXIS,
        ],
    )
    assert default.exit_code == 0, default.output
    assert transposed.exit_code == 0, transposed.output
    default_rows = json.loads(default.output)
    transposed_rows = json.loads(transposed.output)
    # Transposing swaps the two axes: each one's values head the other side.
    assert next(iter(transposed_rows[0])) == "OS"
    assert {row["Python"] for row in default_rows} == set(transposed_rows[0]) - {"OS"}
    assert {row["OS"] for row in transposed_rows} == set(default_rows[0]) - {"Python"}


def test_show_test_matrix_rejects_an_unknown_axis():
    """An axis no job carries is refused, naming the keys the matrix has."""
    result = CliRunner().invoke(repomatic, ["show-test-matrix", "--col-axis", "papaya"])
    assert result.exit_code != 0
    assert "papaya" in result.output
    assert PYTHON_VERSION_AXIS in result.output


def test_show_test_matrix_rejects_unknown_name():
    """An unknown matrix name is rejected by the Choice argument."""
    result = CliRunner().invoke(repomatic, ["show-test-matrix", "bogus"])
    assert result.exit_code != 0


def test_dev_targets_default():
    """Without config, the canary subset is the fastest Linux arm builder."""
    assert Metadata().dev_targets == {"linux-arm64"}


def test_dev_targets_discards_unknown(tmp_path, monkeypatch, caplog):
    """Unknown dev target names are warned about and dropped."""
    metadata = metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        """
[project]
name = "papaya"
version = "1.0.0"

[tool.repomatic]
nuitka.dev-targets = ["windows-x64", "himalaya"]
""",
    )
    with caplog.at_level(logging.WARNING):
        assert metadata.dev_targets == {"windows-x64"}
    assert "himalaya" in caplog.text


def test_nuitka_matrix_canary_on_push(monkeypatch):
    """An ordinary CI push compiles only the dev-targets canary subset."""
    monkeypatch.setattr("repomatic.metadata.is_github_ci", lambda: True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    matrix = Metadata().nuitka_matrix
    assert matrix is not None
    assert matrix["os"] == ("ubuntu-26.04-arm",)
    # Only the canary target's include data is present.
    include_targets = {i["target"] for i in matrix.include if "target" in i}
    assert include_targets == {"linux-arm64"}


def test_nuitka_matrix_full_fleet_on_schedule(monkeypatch):
    """The weekly scheduled run rebuilds every target."""
    monkeypatch.setattr("repomatic.metadata.is_github_ci", lambda: True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    matrix = Metadata().nuitka_matrix
    assert matrix is not None
    assert set(matrix["os"]) == {
        target["os"] for target in NUITKA_BUILD_TARGETS.values()
    }


def test_nuitka_matrix_full_fleet_on_release_push(monkeypatch):
    """A push carrying a release commit rebuilds every target."""
    monkeypatch.setattr("repomatic.metadata.is_github_ci", lambda: True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    release_matrix = Metadata().current_commit_matrix
    monkeypatch.setattr(Metadata, "release_commits_matrix", release_matrix)
    matrix = Metadata().nuitka_matrix
    assert matrix is not None
    assert set(matrix["os"]) == {
        target["os"] for target in NUITKA_BUILD_TARGETS.values()
    }


def test_nuitka_matrix_full_fleet_locally():
    """Outside CI (no event), the matrix keeps the full roster."""
    matrix = Metadata().nuitka_matrix
    assert matrix is not None
    assert set(matrix["os"]) == {
        target["os"] for target in NUITKA_BUILD_TARGETS.values()
    }


def test_nuitka_matrix_skips_push_without_dev_targets(monkeypatch):
    """An empty dev-targets list disables binary builds on ordinary pushes."""
    monkeypatch.setattr("repomatic.metadata.is_github_ci", lambda: True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setattr(Metadata, "dev_targets", set())
    assert Metadata().nuitka_matrix is None


def test_null_sha_constant():
    """Test that NULL_SHA is a valid 40-character string of zeros.

    This constant is used to detect when GitHub sends a null SHA as the "before"
    commit when a tag is created (since there is no previous commit).
    """
    assert isinstance(NULL_SHA, str)
    assert len(NULL_SHA) == 40
    assert NULL_SHA == "0" * 40
    # Verify it's truthy (important for the fix: we can't just check `if not sha`).
    assert bool(NULL_SHA) is True


def test_new_commits_degrades_when_git_rejects_checkout(monkeypatch, caplog):
    """A git failure degrades new_commits to None with git's stderr surfaced,
    instead of crashing the whole `metadata` command.

    Reproduces the compiled-binary self-test running inside a manylinux container
    over a checkout git refuses as "dubious ownership".
    """
    monkeypatch.setattr(Metadata, "commit_range", ("a" * 40, "b" * 40))

    def reject(self, commit_id, **kwargs):
        raise subprocess.CalledProcessError(
            128,
            ["git", "rev-list", "--count", "HEAD"],
            stderr="fatal: detected dubious ownership in repository",
        )

    monkeypatch.setattr(Metadata, "git_deepen", reject)
    with caplog.at_level(logging.WARNING):
        assert Metadata().new_commits is None
    assert "dubious ownership" in caplog.text


def test_changed_files_surfaces_git_stderr(monkeypatch, caplog):
    """A `git diff` failure degrades changed_files to None and logs git's stderr."""
    monkeypatch.setattr(Metadata, "commit_range", ("a" * 40, "b" * 40))

    def reject(start, end):
        raise subprocess.CalledProcessError(
            128,
            ["git", "diff", "--name-only"],
            stderr="fatal: detected dubious ownership in repository",
        )

    monkeypatch.setattr("repomatic.metadata.diff_names", reject)
    with caplog.at_level(logging.WARNING):
        assert Metadata().changed_files is None
    assert "dubious ownership" in caplog.text


def test_is_bot_false_by_default(monkeypatch):
    """Test that is_bot is False when not in a bot context."""
    # Clear CI env vars that could make is_bot return True when tests run on a
    # commit pushed by a bot actor.
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    metadata = Metadata()
    # Outside of bot context, is_bot is False.
    assert isinstance(metadata.is_bot, bool)
    assert metadata.is_bot is False


@pytest.mark.parametrize(
    "prop, envvar, value",
    [
        ("event_name", "GITHUB_EVENT_NAME", "push"),
        ("job_name", "GITHUB_JOB", "sync-labels"),
        ("ref_name", "GITHUB_REF_NAME", "main"),
        ("repo_owner", "GITHUB_REPOSITORY_OWNER", "kdeldycke"),
        ("repo_slug", "GITHUB_REPOSITORY", "kdeldycke/repomatic"),
        ("run_attempt", "GITHUB_RUN_ATTEMPT", "1"),
        ("run_id", "GITHUB_RUN_ID", "123456789"),
        ("run_number", "GITHUB_RUN_NUMBER", "42"),
        ("server_url", "GITHUB_SERVER_URL", "https://github.com"),
        ("sha", "GITHUB_SHA", "abc123def456"),
        ("triggering_actor", "GITHUB_TRIGGERING_ACTOR", "kdeldycke"),
        (
            "workflow_ref",
            "GITHUB_WORKFLOW_REF",
            "kdeldycke/repomatic/.github/workflows/autofix.yaml@refs/heads/main",
        ),
    ],
)
def test_ci_context_properties(monkeypatch, prop, envvar, value):
    """Test CI context properties read from environment variables."""
    monkeypatch.setenv(envvar, value)
    metadata = Metadata()
    assert getattr(metadata, prop) == value


def test_ci_context_defaults(monkeypatch):
    """Test CI context properties return None when env vars are unset."""
    for envvar in (
        "GITHUB_EVENT_NAME",
        "GITHUB_JOB",
        "GITHUB_REF_NAME",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_NUMBER",
        "GITHUB_SHA",
        "GITHUB_TRIGGERING_ACTOR",
        "GITHUB_WORKFLOW_REF",
    ):
        monkeypatch.delenv(envvar, raising=False)
    metadata = Metadata()
    assert metadata.event_name is None
    assert metadata.job_name is None
    assert metadata.ref_name is None
    assert metadata.run_attempt is None
    assert metadata.run_id is None
    assert metadata.run_number is None
    assert metadata.sha is None
    assert metadata.triggering_actor is None
    assert metadata.workflow_ref is None


@pytest.mark.parametrize(
    "prop, envvar",
    [
        ("event_name", "GITHUB_EVENT_NAME"),
        ("job_name", "GITHUB_JOB"),
        ("ref_name", "GITHUB_REF_NAME"),
        ("run_attempt", "GITHUB_RUN_ATTEMPT"),
        ("run_id", "GITHUB_RUN_ID"),
        ("run_number", "GITHUB_RUN_NUMBER"),
        ("sha", "GITHUB_SHA"),
        ("triggering_actor", "GITHUB_TRIGGERING_ACTOR"),
        ("workflow_ref", "GITHUB_WORKFLOW_REF"),
    ],
)
def test_ci_context_empty_is_none(monkeypatch, prop, envvar):
    """Test that empty env var values are normalized to None."""
    monkeypatch.setenv(envvar, "")
    metadata = Metadata()
    assert getattr(metadata, prop) is None


def test_repo_name_derived_from_slug(monkeypatch):
    """Test that repo_name is derived from repo_slug."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "kdeldycke/repomatic")
    metadata = Metadata()
    assert metadata.repo_name == "repomatic"


@pytest.mark.parametrize(
    ("gh_slug", "remote_slug", "expected"),
    [
        pytest.param("owner/papaya", "owner/ignored", "owner/papaya", id="gh-cli"),
        pytest.param(None, "owner/mango", "owner/mango", id="git-remote"),
        pytest.param(None, None, None, id="neither"),
    ],
)
def test_repo_slug_fallback_chain(monkeypatch, gh_slug, remote_slug, expected):
    """Without `GITHUB_REPOSITORY`, the slug falls back to gh, then the remote.

    Both fallbacks are stubbed rather than probed: on a runner with no `gh`
    auth the real chain yields `None`, which is what made the original pair of
    tests assert nothing at all on CI.
    """
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    def fake_gh(args, **kwargs):
        if gh_slug is None:
            msg = "gh is not authenticated"
            raise RuntimeError(msg)
        return f"{gh_slug}\n"

    monkeypatch.setattr("repomatic.metadata.run_gh_command", fake_gh)
    monkeypatch.setattr(
        "repomatic.metadata.get_repo_slug_from_remote", lambda: remote_slug
    )

    assert Metadata().repo_slug == expected


def test_repo_name_fallback_to_gh_cli(monkeypatch):
    """`repo_name` is the trailing segment of a gh-resolved slug."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(
        "repomatic.metadata.run_gh_command", lambda args, **kwargs: "owner/papaya\n"
    )

    assert Metadata().repo_name == "papaya"


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("kdeldycke/awesome-billing", True),
        ("kdeldycke/awesome-falsehood", True),
        ("user/my-awesome-thing", False),
        ("user/regular-repo", False),
    ],
)
def test_is_awesome(monkeypatch, repo, expected):
    """Test that is_awesome detects awesome-* repository names."""
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    assert Metadata().is_awesome is expected


def test_is_awesome_none_slug(monkeypatch):
    """Test that is_awesome returns False when repo_slug is None."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(Metadata, "repo_slug", None)
    assert Metadata().is_awesome is False


def test_repo_owner_fallback_to_slug(monkeypatch):
    """Test that repo_owner falls back to owner from repo_slug."""
    monkeypatch.delenv("GITHUB_REPOSITORY_OWNER", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "kdeldycke/repomatic")
    metadata = Metadata()
    assert metadata.repo_owner == "kdeldycke"


def test_repo_url_composed(monkeypatch):
    """Test that repo_url is composed from server_url and repo_slug."""
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "kdeldycke/repomatic")
    metadata = Metadata()
    assert metadata.repo_url == "https://github.com/kdeldycke/repomatic"


def test_repo_url_fallback(monkeypatch):
    """`repo_url` composes the default server with a gh-resolved slug."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.setattr(
        "repomatic.metadata.run_gh_command", lambda args, **kwargs: "owner/papaya\n"
    )

    assert Metadata().repo_url == "https://github.com/owner/papaya"


def test_repo_url_none_without_a_slug(monkeypatch):
    """No slug from any source leaves the URL undefined rather than malformed."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(Metadata, "repo_slug", None)

    assert Metadata().repo_url is None


def test_server_url_default(monkeypatch):
    """Test that server_url defaults to https://github.com."""
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    metadata = Metadata()
    assert metadata.server_url == "https://github.com"


def test_repomatic_config_defaults(tmp_path, monkeypatch):
    """Test that [tool.repomatic] config properties return sensible defaults."""
    metadata = metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        '[project]\nname = "test-project"\nversion = "1.0.0"\n',
    )
    assert metadata.config.gitignore.location == "./.gitignore"
    assert metadata.config.gitignore.extra_categories == []
    assert metadata.config.gitignore.extra_content == (
        "# Claude Code local files.\n"
        ".claude/scheduled_tasks.lock\n.claude/settings.local.json\n"
        "**/.claude/.cc-writes/\n\n"
        "# Sphinx linkcheck output.\ndocs/_linkcheck/"
    )
    assert metadata.config.dependency_graph.output == "./docs/assets/dependencies.mmd"
    assert metadata.config.dependency_graph.all_groups is True
    assert metadata.config.dependency_graph.all_extras is True
    assert metadata.config.dependency_graph.no_groups == []
    assert metadata.config.dependency_graph.no_extras == []
    assert metadata.config.dependency_graph.level is None
    assert metadata.config.labels.content_rules == {}
    assert metadata.config.labels.extra == []
    assert metadata.config.labels.extra_files == []
    assert metadata.config.labels.file_rules == {}
    assert metadata.config.pypi_package_history == []
    assert metadata.config.notification_unsubscribe is False
    assert metadata.config.awesome_template_sync is True
    assert metadata.config.binaries_sync is True
    assert metadata.config.bumpversion_sync is True
    assert metadata.config.dev_release_sync is True
    assert metadata.config.gitignore.sync is True
    assert metadata.config.labels.sync is True
    assert metadata.config.mailmap_sync is True
    assert metadata.config.setup_guide is True
    assert metadata.config.uv_lock_sync is True
    assert metadata.config.workflow.source_paths is None
    assert metadata.config.workflow.sync is True
    assert metadata.config.exclude == []
    assert metadata.config.include == []
    assert metadata.config.test_matrix.exclude == []
    assert metadata.config.test_matrix.include == []
    assert metadata.config.test_matrix.remove == {}
    assert metadata.config.test_matrix.replace == {}
    assert metadata.config.test_matrix.variations == {}
    assert metadata.config.test_matrix.full_include == []


def test_full_include_flattens_matrix(tmp_path, monkeypatch):
    """`full-include` rows emit as standalone jobs in a flat matrix.

    A full-include cell sharing its ``os``/``python-version`` with a
    shipped-config combination must add a new job, not overwrite that
    combination, and the matrix must serialize as a flat ``{"include": [...]}``
    list so GitHub runs the rows verbatim.
    """
    metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic]
test-matrix.include = [{ "click-version" = "released" }]
test-matrix.full-include = [
  { "os" = "ubuntu-26.04-arm", "python-version" = "3.10", "click-version" = "8.3.1" },
]
""",
    )
    emitted = Metadata().test_matrix.matrix()

    # Flat form: base axes live only inside the include rows, no cross-product.
    assert "os" not in emitted
    assert "exclude" not in emitted
    rows = emitted["include"]

    shipped = {
        "os": "ubuntu-26.04-arm",
        "python-version": "3.10",
        "click-version": "released",
        "state": "stable",
    }
    pinned = {
        "os": "ubuntu-26.04-arm",
        "python-version": "3.10",
        "click-version": "8.3.1",
        "state": "stable",
    }
    # Both coexist: the full-include row added a job instead of overwriting the
    # shipped-config job at the same os/python.
    assert shipped in rows
    assert pinned in rows


def test_full_include_backfills_probe_defaults(tmp_path, monkeypatch):
    """Free-threaded probe jobs inherit the single-key default includes.

    The 3.14t probe is a standalone include pinned to one runner, appended by
    ``solve()`` and never augmented by GitHub's include algorithm. The flat
    full-include matrix backfills the matrix defaults onto it, so it never
    emits an empty ``click-version``/``cloup-version`` that GitHub would expand
    to ``""`` and a downstream run step would mishandle.
    """
    metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic]
test-matrix.include = [
  { "click-version" = "released" },
  { "cloup-version" = "released" },
]
test-matrix.full-include = [
  { "os" = "ubuntu-26.04-arm", "python-version" = "3.14", "click-version" = "main" },
]
""",
    )
    # `.include` is the flat job list, typed as tuple[dict[str, str], ...]; the
    # equivalent matrix()["include"] widens to a union that defeats indexing.
    rows = Metadata().test_matrix.include

    probe = next(r for r in rows if r["python-version"] == "3.14t")
    assert probe == {
        "os": "ubuntu-26.04-arm",
        "python-version": "3.14t",
        "click-version": "released",
        "cloup-version": "released",
        "state": "stable",
    }
    # Every emitted job carries the version defaults: none leaks an empty key
    # that GitHub would expand to "".
    assert all(r.get("click-version") and r.get("cloup-version") for r in rows)


def test_repomatic_config_custom_values(tmp_path, monkeypatch):
    """Test that [tool.repomatic] config properties read from pyproject.toml."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic]
gitignore.location = "./custom/.gitignore"
gitignore.extra-categories = ["terraform", "go"]
gitignore.extra-content = '''
*.tmp

# Claude Code
.claude/
'''
dependency-graph.output = "./custom/deps.mmd"
dependency-graph.all-groups = false
dependency-graph.all-extras = true
dependency-graph.no-groups = ["typing"]
dependency-graph.no-extras = ["xml"]
dependency-graph.level = 2
nuitka.dev-targets = ["macos-arm64", "windows-x64"]
nuitka.unstable-targets = ["linux-arm64", "windows-x64"]
labels.extra-files = ["https://example.com/labels.toml"]
pypi-package-history = ["old-name", "older-name"]
notification.unsubscribe = true
awesome-template.sync = false
binaries.sync = false
bumpversion.sync = false
dev-release.sync = false
gitignore.sync = false
labels.sync = false
mailmap.sync = false
setup-guide = false
uv-lock.sync = false
workflow.source-paths = ["extra_platforms"]
workflow.sync = false
exclude = ["skills", "workflows/debug.yaml", "workflows/autolock.yaml"]
include = ["labels"]

[[tool.repomatic.labels.extra]]
name = "📦 manager: apk"
color = "bfdadc"
description = "apk"

[[tool.repomatic.labels.extra]]
name = "📦 manager: brew"
color = "#bfdadc"
description = "homebrew"

[tool.repomatic.labels.file-rules]
"📦 manager: apk" = ["managers/apk*", "tests/*apk*"]
"📚 docs" = ["docs/**"]

[tool.repomatic.labels.content-rules]
"🔌 bar-plugin" = ["xbar", "swiftbar"]

[tool.repomatic.test-matrix]
exclude = [
    {os = "windows-11-arm"},
    {os = "macos-26-intel", python-version = "3.10"},
]
include = [
    {state = "unstable", python-version = "3.99"},
]

[tool.repomatic.test-matrix.variations]
os = ["custom-runner"]
click-version = ["released", "stable", "main"]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    assert metadata.config.gitignore.location == "./custom/.gitignore"
    assert metadata.config.gitignore.extra_categories == [
        "terraform",
        "go",
    ]
    assert (
        metadata.config.gitignore.extra_content == "*.tmp\n\n# Claude Code\n.claude/\n"
    )
    assert metadata.config.dependency_graph.output == "./custom/deps.mmd"
    assert metadata.config.dependency_graph.all_groups is False
    assert metadata.config.dependency_graph.all_extras is True
    assert metadata.config.dependency_graph.no_groups == ["typing"]
    assert metadata.config.dependency_graph.no_extras == ["xml"]
    assert metadata.config.dependency_graph.level == 2
    assert metadata.dev_targets == {"macos-arm64", "windows-x64"}
    assert metadata.unstable_targets == {"linux-arm64", "windows-x64"}
    assert metadata.config.labels.extra == [
        {
            "name": "📦 manager: apk",
            "color": "bfdadc",
            "description": "apk",
        },
        {
            "name": "📦 manager: brew",
            "color": "#bfdadc",
            "description": "homebrew",
        },
    ]
    assert metadata.config.labels.extra_files == [
        "https://example.com/labels.toml",
    ]
    # Label keys carry emojis, spaces and colons: they must reach the config
    # verbatim, shielded from the loader's key normalization.
    assert metadata.config.labels.file_rules == {
        "📦 manager: apk": ["managers/apk*", "tests/*apk*"],
        "📚 docs": ["docs/**"],
    }
    assert metadata.config.labels.content_rules == {
        "🔌 bar-plugin": ["xbar", "swiftbar"],
    }
    assert metadata.config.pypi_package_history == ["old-name", "older-name"]
    assert metadata.config.notification_unsubscribe is True
    assert metadata.config.awesome_template_sync is False
    assert metadata.config.binaries_sync is False
    assert metadata.config.bumpversion_sync is False
    assert metadata.config.dev_release_sync is False
    assert metadata.config.gitignore.sync is False
    assert metadata.config.labels.sync is False
    assert metadata.config.mailmap_sync is False
    assert metadata.config.setup_guide is False
    assert metadata.config.uv_lock_sync is False
    assert metadata.config.workflow.source_paths == ["extra_platforms"]
    assert metadata.config.workflow.sync is False
    assert metadata.config.exclude == [
        "skills",
        "workflows/debug.yaml",
        "workflows/autolock.yaml",
    ]
    assert metadata.config.include == ["labels"]
    assert metadata.config.test_matrix.exclude == [
        {"os": "windows-11-arm"},
        {"os": "macos-26-intel", "python-version": "3.10"},
    ]
    assert metadata.config.test_matrix.include == [
        {"state": "unstable", "python-version": "3.99"},
    ]
    assert metadata.config.test_matrix.variations == {
        "os": ["custom-runner"],
        "click-version": ["released", "stable", "main"],
    }


def test_test_matrix_config_exclude(tmp_path, monkeypatch):
    """Test that test-matrix.exclude removes combos from both matrices."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic.test-matrix]
exclude = [
    {os = "windows-11-arm"},
]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)

    # Full matrix: config exclude is present alongside the upstream default.
    full = metadata.test_matrix.matrix()
    assert {"os": "windows-11-arm"} in full["exclude"]
    assert {"os": "windows-11-arm", "python-version": "3.10"} in full["exclude"]

    # PR matrix: windows-11-arm is not in the PR runner list, so the exclude
    # is pruned as a no-op.
    pr = metadata.test_matrix_pr.matrix()
    assert "exclude" not in pr or {"os": "windows-11-arm"} not in pr.get("exclude", ())


def test_test_matrix_config_variations(tmp_path, monkeypatch):
    """Test that test-matrix.variations adds to full matrix only."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic.test-matrix.variations]
os = ["custom-runner"]
click-version = ["released", "stable"]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)

    # Full matrix: custom-runner added to OS, click-version is a new axis.
    full = metadata.test_matrix.matrix()
    assert "custom-runner" in full["os"]
    assert "click-version" in full
    assert full["click-version"] == ("released", "stable")

    # PR matrix: variations are NOT applied.
    pr = metadata.test_matrix_pr.matrix()
    assert "custom-runner" not in pr["os"]
    assert "click-version" not in pr


def test_test_matrix_config_include(tmp_path, monkeypatch):
    """Test that test-matrix.include adds directives to both matrices."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic.test-matrix]
include = [
    {state = "unstable", python-version = "3.99"},
]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)

    # Full matrix: custom include added.
    full_includes = metadata.test_matrix.matrix()["include"]
    assert {"state": "unstable", "python-version": "3.99"} in full_includes

    # PR matrix: same custom include added.
    pr_includes = metadata.test_matrix_pr.matrix()["include"]
    assert {"state": "unstable", "python-version": "3.99"} in pr_includes


def test_test_matrix_config_replace(tmp_path, monkeypatch):
    """Test that test-matrix.replace swaps axis values in both matrices."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic.test-matrix.replace]
os = { "ubuntu-26.04-arm" = "ubuntu-24.04" }
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)

    # Full matrix: ubuntu-26.04-arm replaced with ubuntu-24.04.
    full = metadata.test_matrix.matrix()
    assert "ubuntu-24.04" in full["os"]
    assert "ubuntu-26.04-arm" not in full["os"]

    # PR matrix: same replacement applied (ubuntu-26.04-arm is in both sets).
    pr = metadata.test_matrix_pr.matrix()
    assert "ubuntu-24.04" in pr["os"]
    assert "ubuntu-26.04-arm" not in pr["os"]


def test_test_matrix_config_remove(tmp_path, monkeypatch):
    """Test that test-matrix.remove drops axis values from both matrices."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic.test-matrix.remove]
os = ["windows-11-arm"]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)

    # Full matrix: windows-11-arm removed from the axis entirely.
    full = metadata.test_matrix.matrix()
    assert "windows-11-arm" not in full["os"]

    # PR matrix: windows-11-arm was never in the PR runner list, so no change.
    pr = metadata.test_matrix_pr.matrix()
    assert "windows-11-arm" not in pr["os"]

    # Verify no resurrection: unstable python include cannot bring it back.
    full_jobs = list(metadata.test_matrix.solve())
    assert all(j["os"] != "windows-11-arm" for j in full_jobs)


def test_test_matrix_config_unstable(tmp_path, monkeypatch):
    """Test test-matrix.unstable flags full-matrix combos and skips the PR matrix."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic.test-matrix]
variations.click-version = ["released", "colorama"]
unstable = [
    {click-version = "colorama"},
]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)

    # Full matrix: every colorama combo is flagged unstable, on every OS/Python.
    full_jobs = list(metadata.test_matrix.solve())
    colorama_jobs = [j for j in full_jobs if j.get("click-version") == "colorama"]
    assert colorama_jobs
    assert all(j["state"] == "unstable" for j in colorama_jobs)

    # At a stable Python the marking is what flips state: colorama is unstable
    # while released stays stable (released only goes unstable at a development
    # Python, via the separate built-in rule).
    colorama_stable_py = [j for j in colorama_jobs if j["python-version"] == "3.14"]
    released_stable_py = [
        j
        for j in full_jobs
        if j.get("click-version") == "released" and j["python-version"] == "3.14"
    ]
    assert colorama_stable_py and released_stable_py
    assert all(j["state"] == "unstable" for j in colorama_stable_py)
    assert all(j["state"] == "stable" for j in released_stable_py)

    # PR matrix: the directive is full-only, so it is absent and no PR job is
    # hijacked into the colorama click-version or flipped to unstable.
    pr = metadata.test_matrix_pr.matrix()
    assert {"state": "unstable", "click-version": "colorama"} not in pr.get(
        "include", ()
    )
    pr_jobs = list(metadata.test_matrix_pr.solve())
    assert all(j["state"] == "stable" for j in pr_jobs)
    assert all(j.get("click-version") != "colorama" for j in pr_jobs)


def test_single_runner_python_versions_are_stable_and_pinned(tmp_path, monkeypatch):
    """Each released flavor runs once, on its pinned runner, stable; never in PR.

    Locks the SINGLE_RUNNER_PYTHON_VERSIONS contract for every entry: a
    free-threaded build is a single-runner smoke test, not the full
    cross-platform spread, and not an unstable probe.
    """
    metadata = metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        '[project]\nname = "test-project"\nversion = "1.0.0"\n',
    )

    full_jobs = list(metadata.test_matrix.solve())
    pr_jobs = list(metadata.test_matrix_pr.solve())
    assert SINGLE_RUNNER_PYTHON_VERSIONS
    for version, runner in SINGLE_RUNNER_PYTHON_VERSIONS.items():
        # A flavor is neither part of the full cross-platform spread nor an
        # unstable probe: it is released and expected to pass.
        assert version not in TEST_PYTHON_FULL
        assert version not in UNSTABLE_PYTHON_VERSIONS
        # Exactly one full-matrix job, on the pinned runner, marked stable.
        assert [j for j in full_jobs if j["python-version"] == version] == [
            {"os": runner, "python-version": version, "state": "stable"}
        ]
        # The reduced PR matrix never carries a flavor.
        assert all(j["python-version"] != version for j in pr_jobs)


def test_unstable_python_versions_carry_a_prerelease_label(tmp_path, monkeypatch):
    """Each in-development Python gets a display label; the axis keeps the version.

    The label is what the CI job name shows, so an unstable cell reads
    `py3.15-dev` and says why it may fail rather than looking like any released
    one. Adding a version to UNSTABLE_PYTHON_VERSIONS and forgetting its label
    is the failure this catches.

    Label and axis value stay separate on purpose: `uv venv --python`, which
    `tests.yaml` feeds from `python-version`, does not parse the `-dev`
    spelling, and downstream `[tool.repomatic.test-matrix]` directives key on
    the bare version.
    """
    metadata = metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        '[project]\nname = "test-project"\nversion = "1.0.0"\n',
    )

    full_jobs = list(metadata.test_matrix.solve())
    pr_jobs = list(metadata.test_matrix_pr.solve())
    assert UNSTABLE_PYTHON_VERSIONS
    for version in UNSTABLE_PYTHON_VERSIONS:
        # The axis carries the bare version, never the labelled spelling.
        assert version in TEST_PYTHON_FULL
        assert f"{version}{PRERELEASE_LABEL_SUFFIX}" not in TEST_PYTHON_FULL
        probes = [j for j in full_jobs if j["python-version"] == version]
        assert probes
        for job in probes:
            assert job["state"] == "unstable"
            assert job["python-label"] == f"{version}{PRERELEASE_LABEL_SUFFIX}"
        # The reduced PR matrix never probes an unreleased Python.
        assert all(j["python-version"] != version for j in pr_jobs)

    # Labelled and unstable are the same set of cells: a released Python must
    # stay unlabelled so the job name falls back to its own version.
    assert all(
        ("python-label" in job) == (job["python-version"] in UNSTABLE_PYTHON_VERSIONS)
        for job in full_jobs
    )


def test_stale_test_matrix_excludes(tmp_path, monkeypatch):
    """Test detection of test-matrix.exclude entries that match no live axis."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic.test-matrix]
exclude = [
    {os = "ubuntu-26.04"},
    {os = "macos-15-intel"},
    {python-version = "9.99"},
]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)

    # ubuntu-26.04 is a live axis value, so its exclude is honored. The renamed
    # macos-15-intel and the bogus Python version match no axis and are flagged.
    # `ubuntu-slim` would fail here too: a known runner, but no longer a cell.
    stale = metadata.stale_test_matrix_excludes
    assert {"os": "ubuntu-26.04"} not in stale
    assert {"os": "macos-15-intel"} in stale
    assert {"python-version": "9.99"} in stale


def test_unstable_targets_default(tmp_path, monkeypatch):
    """Test that unstable_targets defaults to an empty set."""
    metadata = metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        '[project]\nname = "test-project"\nversion = "1.0.0"\n',
    )
    assert metadata.unstable_targets == set()


def test_unstable_targets_ignores_unknown(tmp_path, monkeypatch):
    """Test that unrecognized unstable targets are discarded with a warning."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic]
nuitka.unstable-targets = ["linux-arm64", "unknown-target"]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    # Only known targets are kept.
    assert metadata.unstable_targets == {"linux-arm64"}


def test_nuitka_entry_points_default_deduplicates(tmp_path, monkeypatch):
    """Default: alias entry points sharing a callable are deduplicated."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
short = "my_pkg.__main__:main"
long-name = "my_pkg.__main__:main"
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    assert metadata.config.nuitka_entry_points == []
    # Both point to the same callable, so only the first is kept.
    assert metadata.nuitka_entry_points == ["short"]


def test_nuitka_entry_points_default_distinct(tmp_path, monkeypatch):
    """Default: entry points with different callables are all kept."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
cli-a = "my_pkg.cli:main_a"
cli-b = "my_pkg.cli:main_b"
cli-a-alias = "my_pkg.cli:main_a"
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    # cli-a and cli-b have different callables, both kept.
    # cli-a-alias duplicates cli-a's callable, dropped.
    assert metadata.nuitka_entry_points == ["cli-a", "cli-b"]


def test_nuitka_entry_points_custom(tmp_path, monkeypatch):
    """Custom entry-points config selects specific entry points."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
short = "my_pkg.__main__:main"
long-name = "my_pkg.__main__:main"

[tool.repomatic]
nuitka.entry-points = ["long-name"]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    assert metadata.nuitka_entry_points == ["long-name"]


def test_nuitka_entry_points_all(tmp_path, monkeypatch):
    """Listing all entry points explicitly builds all of them."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
short = "my_pkg.__main__:main"
long-name = "my_pkg.__main__:main"

[tool.repomatic]
nuitka.entry-points = ["short", "long-name"]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    assert metadata.nuitka_entry_points == ["short", "long-name"]


def test_nuitka_entry_points_ignores_unknown(tmp_path, monkeypatch):
    """Unrecognized entry points are discarded; falls back to first if all invalid."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
short = "my_pkg.__main__:main"

[tool.repomatic]
nuitka.entry-points = ["nonexistent"]
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    # All configured entries are invalid, so falls back to first.
    assert metadata.nuitka_entry_points == ["short"]


def test_script_entries_basic(tmp_path, monkeypatch):
    """Well-formed `[project.scripts]` entries parse into (name, module, callable)."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
mdedup = "mail_deduplicate.cli:mdedup"
mpm = "meta_package_manager.__main__:main"
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    assert metadata.script_entries == [
        ("mdedup", "mail_deduplicate.cli", "mdedup"),
        ("mpm", "meta_package_manager.__main__", "main"),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "nested/script",
        ".",
        "..",
        "with space",
        "exclaim!",
    ],
)
def test_script_entries_rejects_unsafe_name(tmp_path, monkeypatch, name):
    """Script names that PyPI / uv-build would refuse are rejected up front."""
    pyproject_content = f"""\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
"{name}" = "my_pkg.cli:main"
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    with pytest.raises(ValueError, match=r"\[project\.scripts\] name"):
        _ = metadata.script_entries


@pytest.mark.parametrize(
    "value",
    [
        "no_colon",
        "a:b:c",
        ":missing_module",
        "missing_callable:",
        "",
    ],
)
def test_script_entries_rejects_malformed_value(tmp_path, monkeypatch, value):
    """Values not of the form `module:object` raise a descriptive error."""
    pyproject_content = f"""\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
cli = "{value}"
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    with pytest.raises(ValueError, match=r"\[project\.scripts\] value"):
        _ = metadata.script_entries
