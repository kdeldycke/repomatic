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

"""Python-project reading of {class}`~repomatic.metadata.core.Metadata`.

Everything derived from `pyproject.toml` and the Sphinx configuration:
project identity, entry points, version state, and tool parameters.
"""

from __future__ import annotations

import ast
import logging
import re
from functools import cached_property
from pathlib import Path

import tomlrt
from packaging.version import Version
from pyproject_metadata import ConfigurationError, StandardMetadata

from ..config import (
    Config,
    load_repomatic_config,
)
from ..git_ops import (
    get_latest_tag_version,
    get_release_version_from_commits,
)
from ..pyproject import (
    is_python_package as _is_python_package,
    is_python_project as _is_python_project,
)
from ..release.binary import NUITKA_BUILD_TARGETS
from ..tooling.tool_registry import MYPY_VERSION_MIN

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final, Literal

    from ..github.matrix import Matrix


_SCRIPT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]+")
"""Allowed characters in a `[project.scripts]` entry name.

Matches the rule PyPI enforces on uploaded wheels and the validation
[uv-build performs](https://github.com/astral-sh/uv/pull/19495) before
writing wheel metadata. Names are also required to be non-empty and to
contain at least one non-dot character; both extra checks live next to
the regex in {meth}`ProjectMetadata.script_entries`.
"""


def _known_build_targets(names: list[str], kind: str) -> set[str]:
    """Keep the recognized Nuitka build targets among *names*.

    Shared by {attr}`ProjectMetadata.dev_targets` and
    {attr}`ProjectMetadata.unstable_targets`, which read two different config lists
    against the same roster and would otherwise drift on how they treat a name
    that roster does not carry.

    :param names: Target names, as configured.
    :param kind: What the list configures, for the warning naming the strays.
    :return: The subset of *names* present in
        {data}`~repomatic.release.binary.NUITKA_BUILD_TARGETS`.
    """
    targets = set(names)
    unknown = targets - set(NUITKA_BUILD_TARGETS)
    if unknown:
        logging.warning(f"Unrecognized {kind} targets: {unknown}")
    return targets & set(NUITKA_BUILD_TARGETS)


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

    current_version_str = ProjectMetadata.get_current_version()
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


class ProjectMetadata:
    """What `pyproject.toml` and the Sphinx configuration declare.

    A concern mixin of {class}`~repomatic.metadata.core.Metadata`: never
    instantiated on its own, and reads sibling concerns through `self`.
    """

    if TYPE_CHECKING:
        # Sibling-concern surface read through `self`: each stub mirrors
        # the descriptor another mixin (or the assembled `Metadata`
        # class) defines, so every concern type-checks on its own.
        pyproject_path: Path
        sphinx_conf_path: Path

        @cached_property
        def new_commits_matrix(self) -> Matrix | None:
            """See {class}`~repomatic.metadata.git.GitMetadata`."""

        @cached_property
        def release_commits_matrix(self) -> Matrix | None:
            """See {class}`~repomatic.metadata.git.GitMetadata`."""

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
