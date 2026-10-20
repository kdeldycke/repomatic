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
{class}`~repomatic.metadata.core.Metadata` singleton and can be used independently.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tomlrt
from packaging.utils import canonicalize_name
from pyproject_metadata import ConfigurationError, StandardMetadata

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any, Final

    from .config import Config


BUMPVERSION_SOURCES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (".bumpversion.toml", ()),
    ("pyproject.toml", ("tool", "bumpversion")),
)
"""Where bump-my-version reads its configuration, in the order it searches.

Each entry pairs a file name with the path of the table holding
`current_version` in it: the top level of `.bumpversion.toml`, or the
`[tool.bumpversion]` table of `pyproject.toml`.
"""

_TOML_CACHE: dict[Path, tuple[int, int, Any]] = {}
"""Parsed TOML documents, keyed by absolute path.

Each value pairs the parse with the `(st_mtime_ns, st_size)` identity of the
bytes it came from, so an edited file (a test rewriting its fixture, a sync
job upserting a key, `uv lock` rewriting the lock) re-parses, while the dozens
of reads a single invocation makes hit the cache.
"""

_EMPTY_PYPROJECT: dict[str, Any] = {}
"""The one mapping every missing or unreadable `pyproject.toml` reads as.

Returning a shared object rather than a fresh `{}` gives the no-file case a
stable identity, which is what `load_repomatic_config`'s memo keys on: a
repository carrying no `pyproject.toml` at all (an awesome list) would
otherwise rebuild its default `Config` on every read. Same read-only contract
as the cached parses above.
"""


def cached_toml(path: Path) -> Any:
    """Parse the TOML file at *path*, once per version of its bytes.

    The one parser behind {func}`read_pyproject_toml` and
    {meth}`repomatic.deps.lockfile.LockFile.load`: a lock runs to hundreds of
    kilobytes, and a single command reads the same `pyproject.toml` many times
    over.

    ```{caution}
    The `tomlrt` document is shared by every caller, so treat it as read-only.
    It keeps the formatting trivia of the file (the tool runner relies on it),
    which a caller editing the file must not touch: {func}`load_pyproject_doc`
    returns a private, editable parse instead.
    ```

    :param path: The TOML file to read.
    :return: The parsed document.
    :raises OSError: When the file is missing or cannot be read.
    :raises tomlrt.TOMLParseError: When the file is not valid TOML.
    """
    path = path.absolute()
    stat = path.stat()
    cached = _TOML_CACHE.get(path)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]
    document = tomlrt.loads(path.read_text(encoding="UTF-8"))
    _TOML_CACHE[path] = (stat.st_mtime_ns, stat.st_size, document)
    return document


def read_pyproject_toml(project_root: Path | None = None) -> dict[str, Any]:
    """Parse `pyproject.toml` from *project_root*.

    Parses are cached per file identity (see {func}`cached_toml`), since a
    single CLI invocation reads the same document many times over: treat the
    result as read-only.

    :param project_root: Directory holding `pyproject.toml`. Defaults to the
        current working directory.
    :return: Parsed contents, or an empty dict when the file is missing or
        cannot be decoded.
    """
    if project_root is None:
        project_root = Path()
    pyproject_path = project_root / "pyproject.toml"
    # `OSError` covers a file that is missing, unreadable, a directory, or gone
    # between the stat and the read (the default *project_root* is relative, so
    # the working directory itself can vanish mid-call). The missing-file
    # outcome this promises is the same either way.
    try:
        data: dict[str, Any] = cached_toml(pyproject_path)
    except (tomlrt.TOMLParseError, OSError):
        return _EMPTY_PYPROJECT
    return data


def load_pyproject_doc(pyproject_path: Path) -> Any:
    """Parse a TOML file into a private, editable, round-trippable document.

    The counterpart to {func}`read_pyproject_toml`, whose cached document is
    shared and read-only. This one is parsed afresh on every call and keeps
    `tomlrt`'s formatting trivia, so the document can be edited and written back
    with the rest of the file byte-identical.

    :param pyproject_path: Path to the `pyproject.toml` (or other TOML) file.
    :return: The parsed document.
    """
    return tomlrt.loads(pyproject_path.read_text(encoding="UTF-8"))


def bumpversion_current_version(
    document: Mapping[str, Any], table_path: tuple[str, ...]
) -> str | None:
    """Read `current_version` from the bump-my-version table of a TOML document.

    :param document: A parsed TOML document.
    :param table_path: The keys leading to the table, as
        {data}`BUMPVERSION_SOURCES` gives them.
    :return: The version, or `None` when the table or the key is missing.
    """
    table: Any = document
    for key in table_path:
        table = table.get(key)
        if not isinstance(table, dict):
            return None
    version = table.get("current_version")
    return None if version is None else str(version)


def get_current_version(project_root: Path | None = None) -> str | None:
    """Returns the current version as managed by bump-my-version.

    Same as calling the CLI:

    ```{code-block} shell-session
    $ bump-my-version show current_version
    ```

    Reads the first file of {data}`BUMPVERSION_SOURCES` that declares a
    version. Each parse is cached per version of the file's bytes, see
    {func}`cached_toml`.

    :param project_root: Directory holding the configuration files. Defaults to
        the current working directory.
    :return: The version, or `None` when no file declares one.
    """
    root = Path() if project_root is None else project_root
    for filename, table_path in BUMPVERSION_SOURCES:
        try:
            document = cached_toml(root / filename)
        except (tomlrt.TOMLParseError, OSError):
            continue
        version = bumpversion_current_version(document, table_path)
        if version is not None:
            return version
    return None


def uv_table(doc: Any) -> Any:
    """Return the `[tool.uv]` table of a parsed `pyproject.toml`.

    :param doc: Document from {func}`load_pyproject_doc`.
    :return: The `[tool.uv]` table, or an empty mapping when the project
        declares none. Reading a key off the result is therefore always safe;
        writing one back requires the caller to check the table exists first,
        since the empty fallback is not attached to *doc*.
    """
    return doc.get("tool", {}).get("uv", {})


def dependency_group_names(project_root: Path | None = None) -> tuple[str, ...]:
    """Every group name declared in `[dependency-groups]`, sorted.

    :param project_root: Directory holding `pyproject.toml`. Defaults to the
        current working directory.
    :return: Group names, empty when the project declares none.
    """
    groups = read_pyproject_toml(project_root).get("dependency-groups") or {}
    return tuple(sorted(groups))


def extra_names(project_root: Path | None = None) -> tuple[str, ...]:
    """Every extra declared in `[project.optional-dependencies]`, sorted.

    :param project_root: Directory holding `pyproject.toml`. Defaults to the
        current working directory.
    :return: Extra names, empty when the project declares none.
    """
    project = read_pyproject_toml(project_root).get("project") or {}
    return tuple(sorted(project.get("optional-dependencies") or {}))


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
        return configured or None
    derived = derive_source_paths(pyproject_data)
    return derived or None


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
    {attr}`~repomatic.components.RepoScope.PYTHON_ONLY`. It has nothing to
    publish, tag or write release notes for, so it wants nothing scoped to
    {attr}`~repomatic.components.RepoScope.PACKAGE_ONLY`.

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
