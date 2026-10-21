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
from typing import NamedTuple

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
    get_current_version,
    is_python_package as _is_python_package,
    is_python_project as _is_python_project,
    read_pyproject_toml,
)
from ..versions import BUMP_PARTS

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final, Literal

    import packaging.version


MYPY_VERSION_MIN = (3, 10)
"""Earliest Python dialect the pinned mypy accepts for `--python-version`.

Floors the value {attr}`ProjectMetadata.mypy_params` derives from the project's
`requires-python`, which the `mypy` entry of
{data}`~repomatic.tooling.registry.TOOL_REGISTRY` passes through
`computed_params`. A project declaring an older floor would otherwise hand mypy
a version it refuses with exit code `2`. Mypy's own `PYTHON3_VERSION_MIN`
holds the value, in
[`mypy/defaults.py`](https://github.com/python/mypy/blob/master/mypy/defaults.py).
"""


MANPAGES_ASSET_SUFFIX: Final[str] = "-manpages"
"""What the package name takes to name the man-page tarball, by default."""


_SCRIPT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]+")
"""Allowed characters in a `[project.scripts]` entry name.

Matches the rule PyPI enforces on uploaded wheels and the validation
[uv-build performs](https://github.com/astral-sh/uv/pull/19495) before
writing wheel metadata. Names are also required to be non-empty and to
contain at least one non-dot character; both extra checks live next to
the regex in {meth}`ProjectMetadata.script_entries`.
"""


class BumpBaseline(NamedTuple):
    """The two versions a version-bump gate compares."""

    # Spelled in full: Sphinx renders the signature of a named tuple from
    # its generated `__new__`, which reads no import of this module.
    current: packaging.version.Version
    """The version the bump-my-version configuration declares now."""

    released: packaging.version.Version
    """The latest release, read from its tag or from its release commit."""

    @classmethod
    def resolve(cls) -> BumpBaseline | None:
        """Read both versions from the working directory and its git history.

        The latest release comes from the tags first. When none is found, as on
        a shallow clone or while the release workflow has not pushed its tag
        yet, it comes from the most recent release commit message.

        :return: The baseline, or `None` when either version is unknown: a gate
            with nothing to compare against allows the bump.
        """
        current = get_current_version()
        if not current:
            logging.warning("Cannot determine current version. Allowing bump.")
            return None
        released = get_latest_tag_version()
        if not released:
            logging.info("No tags found, falling back to commit message parsing.")
            released = get_release_version_from_commits()
        if not released:
            logging.warning(
                "No release version found from tags or commits. Allowing bump."
            )
            return None
        baseline = cls(Version(current), released)
        logging.info(f"Current version: {baseline.current}, Latest release: {released}")
        return baseline

    def allows(self, part: Literal["minor", "major"]) -> bool:
        """Whether a bump of *part* would not increment it twice in one cycle.

        - Last release `5.0.1`, current `5.0.2`: a minor bump is allowed.
        - Last release `5.0.1`, current `5.1.0`: the minor was already bumped.
        - Last release `5.0.1`, current `6.0.0`: the major was already bumped,
          which blocks both parts.

        :param part: The version part to bump.
        :return: `True` if the bump should proceed.
        """
        if self.current.major > self.released.major:
            logging.info(
                "Major version already bumped "
                f"({self.current.major} > {self.released.major}). Skipping bump."
            )
            return False
        if (
            part == "minor"
            and self.current.major == self.released.major
            and self.current.minor > self.released.minor
        ):
            logging.info(
                "Minor version already bumped "
                f"({self.current.minor} > {self.released.minor}). Skipping bump."
            )
            return False
        logging.info(f"Version bump for {part} is allowed.")
        return True


def is_version_bump_allowed(part: Literal["minor", "major"]) -> bool:
    """Check if a version bump of the specified part is allowed.

    This prevents double version increments within a development cycle. A bump is
    blocked if the version has already been bumped (but not released) since the last
    tagged release. {meth}`BumpBaseline.allows` holds the rule.

    :param part: The version part to check (`minor` or `major`).
    :return: `True` if the bump should proceed, `False` if it should be skipped.
    :raises ValueError: When *part* is not a bumpable part.
    """
    if part not in BUMP_PARTS:
        expected = " or ".join(repr(p) for p in BUMP_PARTS)
        raise ValueError(f"Invalid version part: {part!r}. Must be {expected}.")
    baseline = BumpBaseline.resolve()
    return baseline is None or baseline.allows(part)


class ProjectMetadata:
    """What `pyproject.toml` and the Sphinx configuration declare.

    A concern mixin of {class}`~repomatic.metadata.core.Metadata`: never
    instantiated on its own.
    """

    pyproject_path = Path() / "pyproject.toml"
    """The project file every property here reads."""

    sphinx_conf_path = Path() / "docs" / "conf.py"
    """The Sphinx configuration file."""

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
        {attr}`~repomatic.components.RepoScope.PACKAGE_ONLY` resolves against, so
        the release lane and the checks that police it agree on who publishes.

        Prefer this over the truthiness of {attr}`package_name` when gating
        anything about publishing. `package_name` only reports what `[project]
        name` says, which a virtual project still declares.
        """
        return _is_python_package(pyproject_data=self.pyproject_toml)

    @cached_property
    def pyproject_toml(self) -> dict[str, Any]:
        """Returns the parsed content of `pyproject.toml`.

        The cached document {func}`~repomatic.pyproject.read_pyproject_toml`
        shares with every other reader of the file, so treat it as read-only.
        Empty when the file does not exist or does not parse.
        """
        return read_pyproject_toml(self.pyproject_path.parent)

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

        Merges user configuration with defaults from `Config`, through
        {func}`~repomatic.config.load_repomatic_config`: for the project in the
        working directory, the configuration the running command published
        wins over the file.
        """
        return load_repomatic_config(project_root=self.pyproject_path.parent)

    @cached_property
    def package_name(self) -> str | None:
        """Returns package name as published on PyPI."""
        if self.pyproject and self.pyproject.canonical_name:
            return self.pyproject.canonical_name
        return None

    @cached_property
    def manpages_asset_name(self) -> str | None:
        """Filename stem of the man-page tarball attached to a release.

        `[tool.repomatic] manpages.asset-name` when set, `<package-name>-manpages`
        otherwise. `None` for a project with neither.
        """
        if self.config.manpages_asset_name:
            return self.config.manpages_asset_name
        if self.package_name:
            return f"{self.package_name}{MANPAGES_ASSET_SUFFIX}"
        return None

    @cached_property
    def project_description(self) -> str | None:
        """Returns project description from pyproject.toml."""
        if self.pyproject and self.pyproject.description:
            return self.pyproject.description
        return None

    @cached_property
    def script_entries(self) -> list[tuple[str, str, str]]:
        """The `(name, module, callable)` tuple of each console script.

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
        return entries

    @cached_property
    def main_cli_script(self) -> str | None:
        """The first `[project.scripts]` entry, the CLI the self-tests drive.

        :return: The script name, or `None` when the project declares none.
        """
        if not self.script_entries:
            return None
        return self.script_entries[0][0]

    @cached_property
    def import_name(self) -> str | None:
        """The top-level package that `python -m` runs.

        Read from the module of {attr}`main_cli_script`, since the CLI lives in
        that package: `mail_deduplicate.cli` gives `mail_deduplicate`. A project
        with no script falls back to its package name, with each hyphen turned
        into an underscore.

        :return: The import name, or `None` when the project has no name.
        """
        if self.script_entries:
            return self.script_entries[0][1].partition(".")[0]
        if self.package_name:
            return self.package_name.replace("-", "_")
        return None

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

    @cached_property
    def is_sphinx(self) -> bool:
        """Returns `True` if the Sphinx config file is present."""
        # The Sphinx config file is present, that's enough for us.
        return self.sphinx_conf_path.is_file()

    @cached_property
    def builds_sphinx(self) -> bool:
        """Whether the Docs workflow builds this repository's Sphinx site.

        The build installs the project to run Sphinx, so it needs a Python
        project as well as a Sphinx configuration.
        """
        return self.is_python_project and self.is_sphinx

    @cached_property
    def sphinx_extensions(self) -> frozenset[str]:
        """The extensions the Sphinx configuration lists, read once.

        Parses `conf.py` as an AST rather than running it, and reads every
        top-level assignment to the `extensions` variable: a plain or annotated
        one sets the list, an augmented one (`+=`) extends it. An assignment to
        anything other than the bare name, like a tuple target or a subscript,
        is not the extension list and is passed over.

        :return: The extension module names, empty when there is no Sphinx
            configuration or it does not parse.
        """
        if not self.is_sphinx:
            return frozenset()
        try:
            tree = ast.parse(self.sphinx_conf_path.read_bytes())
        except (OSError, SyntaxError, ValueError) as error:
            logging.warning(f"Cannot read {self.sphinx_conf_path}: {error}")
            return frozenset()
        extensions: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets: list[ast.expr] = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            else:
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "extensions"
                for target in targets
            ) or not isinstance(node.value, (ast.List, ast.Tuple)):
                continue
            names = {
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
            if isinstance(node, ast.AugAssign):
                extensions |= names
            else:
                extensions = names
        return frozenset(extensions)

    @cached_property
    def active_autodoc(self) -> bool:
        """Returns `True` if Sphinx autodoc is active."""
        return "sphinx.ext.autodoc" in self.sphinx_extensions

    @cached_property
    def uses_myst(self) -> bool:
        """Returns `True` if MyST-Parser is active in Sphinx."""
        return "myst_parser" in self.sphinx_extensions
