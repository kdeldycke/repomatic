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

"""Utilities for reading and interpreting `pyproject.toml` metadata.

Provides standalone functions for extracting project name and source paths
from `pyproject.toml`. These functions have no dependency on the
{class}`~repomatic.metadata.Metadata` singleton and can be used independently.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tomlrt
from packaging.utils import canonicalize_name
from pyproject_metadata import ConfigurationError, StandardMetadata

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

    from .config import Config


_PARSE_CACHE: dict[Path, tuple[int, int, dict[str, Any]]] = {}
"""Parsed `pyproject.toml` documents, keyed by absolute path.

Each value pairs the parse with the `(st_mtime_ns, st_size)` identity of the
bytes it came from, so an edited file (a test rewriting its fixture, a sync
job upserting a key) re-parses while the dozens of reads a single invocation
makes (every `load_repomatic_config()` call re-reads the file) hit the cache.
Safe to share unguarded: no consumer mutates the returned mapping, and
click-extra's schema callable copies its input before normalizing it.
"""


def read_pyproject_toml(project_root: Path | None = None) -> dict[str, Any]:
    """Parse `pyproject.toml` from *project_root*.

    Parses are cached per file identity (absolute path, mtime, size), since a
    single CLI invocation reads the same document many times over: treat the
    result as read-only.

    :param project_root: Directory holding `pyproject.toml`. Defaults to the
        current working directory.
    :return: Parsed contents, or an empty dict when the file is missing or
        cannot be decoded.
    """
    if project_root is None:
        project_root = Path()
    pyproject_path = (project_root / "pyproject.toml").absolute()
    # `OSError` from the stat or the read covers a file that is missing,
    # unreadable, or gone between the two calls (the default *project_root* is
    # relative, so the working directory itself can vanish mid-call). The
    # missing-file outcome this promises is the same either way.
    try:
        stat = pyproject_path.stat()
        if not pyproject_path.is_file():
            return {}
        cached = _PARSE_CACHE.get(pyproject_path)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        data: dict[str, Any] = tomlrt.loads(pyproject_path.read_text(encoding="UTF-8"))
    except (tomlrt.TOMLParseError, OSError):
        return {}
    _PARSE_CACHE[pyproject_path] = (stat.st_mtime_ns, stat.st_size, data)
    return data


def derive_source_paths(
    pyproject_data: dict[str, Any] | None = None,
) -> list[str]:
    """Derive source code directory name from `[project.name]`.

    Converts the project name to its importable form by replacing hyphens with
    underscores, the universal Python convention that all build backends
    (setuptools, hatchling, flit, uv) follow by default. For example,
    `name = "extra-platforms"` yields `["extra_platforms"]`.

    :param pyproject_data: Pre-parsed `pyproject.toml` dict. If `None`,
        reads from the current working directory.
    :return: Single-element list with the source directory name, or an empty
        list if no project name is defined.
    """
    if pyproject_data is None:
        pyproject_data = read_pyproject_toml()

    name = pyproject_data.get("project", {}).get("name")
    if not name:
        return []
    # PEP 503 normalization (lowercases, collapses [-_.] to hyphens), then
    # convert to the Python import form (underscores).
    return [canonicalize_name(name).replace("-", "_")]


def resolve_source_paths(
    config: Config,
    pyproject_data: dict[str, Any] | None = None,
) -> list[str] | None:
    """Resolve workflow source paths from config or auto-derivation.

    :param config: Loaded `Config` instance from `[tool.repomatic]`.
    :param pyproject_data: Pre-parsed `pyproject.toml` dict for derivation.
    :return: List of source directory names, or `None` when no source paths
        can be determined (paths should be stripped entirely).
    """
    configured = config.workflow.source_paths
    if configured is not None:
        return configured if configured else None
    derived = derive_source_paths(pyproject_data)
    return derived if derived else None


def get_project_name(
    pyproject_data: dict[str, Any] | None = None,
) -> str | None:
    """Read the project name from `pyproject.toml`.

    :param pyproject_data: Pre-parsed dict. If `None`, reads from CWD.
    """
    if pyproject_data is None:
        pyproject_data = read_pyproject_toml()
    name: str | None = pyproject_data.get("project", {}).get("name")
    if name:
        logging.debug(f"Project name from pyproject.toml: {name}")
    return name


def is_python_project(
    project_root: Path | None = None,
    pyproject_data: dict[str, Any] | None = None,
) -> bool:
    """Detect whether *project_root* hosts a Python project.

    Returns `True` when the `pyproject.toml` parses cleanly through
    `pyproject_metadata.StandardMetadata.from_pyproject`: it must declare a
    PEP 621 `[project]` table that respects the standard. A `pyproject.toml`
    that only carries third-party `[tool.*]` sections does not qualify, so
    repositories that merely lean on the file for tool configuration (linters,
    formatters, `[tool.repomatic]` itself) are correctly classified as
    non-Python.

    :param project_root: Directory to probe. Ignored when *pyproject_data* is
        supplied; otherwise defaults to the current working directory.
    :param pyproject_data: Pre-parsed `pyproject.toml`. Pass this when the
        caller has already parsed the file (e.g., the `Metadata` singleton).
    :return: `True` when the `[project]` table satisfies PEP 621.
    """
    if pyproject_data is None:
        pyproject_data = read_pyproject_toml(project_root)
    if not pyproject_data:
        return False
    try:
        StandardMetadata.from_pyproject(pyproject_data)
    except ConfigurationError:
        return False
    return True


def is_python_package(
    project_root: Path | None = None,
    pyproject_data: dict[str, Any] | None = None,
) -> bool:
    """Detect whether *project_root* builds a distributable Python package.

    Strictly narrower than {func}`is_python_project`: every package is a Python
    project, but not every Python project is a package. A uv *virtual project*
    declares a PEP 621 `[project]` table purely to carry dependencies, and opts
    out of being built or installed with `[tool.uv] package = false`. Blogs,
    docs sites and dotfiles repos that lean on uv for dependency management all
    look like this.

    The distinction matters because the two traits gate different things. A
    virtual project still has dependencies to lock, a `uv.lock` to sync and
    tests to cover, so it wants everything scoped to
    {attr}`~repomatic.registry.RepoScope.PYTHON_ONLY`. It has nothing to
    publish, tag or write release notes for, so it wants nothing scoped to
    {attr}`~repomatic.registry.RepoScope.PACKAGE_ONLY`.

    ```{note}
    Only uv's opt-out is recognized. Poetry's `[tool.poetry] package-mode`
    equivalent is deliberately ignored: repomatic dropped Poetry support in
    `4.0.0` and expects standard `pyproject.toml` conventions.
    ```

    :param project_root: Directory to probe. Ignored when *pyproject_data* is
        supplied; otherwise defaults to the current working directory.
    :param pyproject_data: Pre-parsed `pyproject.toml`. Pass this when the
        caller has already parsed the file.
    :return: `True` for a PEP 621 project that is not a uv virtual project.
    """
    if pyproject_data is None:
        pyproject_data = read_pyproject_toml(project_root)
    if not is_python_project(pyproject_data=pyproject_data):
        return False
    return pyproject_data.get("tool", {}).get("uv", {}).get("package") is not False
