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

"""Tests for pyproject.toml utilities."""

from __future__ import annotations

from textwrap import dedent

import pytest
import tomlrt

from repomatic.deps.lockfile import LockFile
from repomatic.pyproject import (
    cached_toml,
    dependency_group_names,
    extra_names,
    get_current_version,
    get_project_name,
    is_python_package,
    is_python_project,
    load_pyproject_doc,
    read_pyproject_toml,
    uv_table,
)


def test_get_project_name_from_cwd(tmp_path, monkeypatch):
    """Test that get_project_name reads from pyproject.toml in CWD."""
    pyproject_content = """\
[project]
name = "my-package"
version = "1.0.0"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    assert get_project_name() == "my-package"


def test_get_project_name_missing_pyproject(tmp_path, monkeypatch):
    """Test that get_project_name returns None when no pyproject.toml."""
    monkeypatch.chdir(tmp_path)
    assert get_project_name() is None


def test_get_project_name_no_project_section(tmp_path, monkeypatch):
    """Test that get_project_name returns None when no [project] section."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    assert get_project_name() is None


def test_get_project_name_with_preloaded_data():
    """Test that get_project_name accepts pre-parsed pyproject data."""
    data = {"project": {"name": "preloaded-pkg"}}
    assert get_project_name(data) == "preloaded-pkg"


def test_is_python_project_true_for_pep621(tmp_path):
    """A PEP 621-compliant `[project]` table qualifies as a Python project."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "orange-grove"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )
    assert is_python_project(tmp_path) is True


def test_is_python_project_false_for_tool_only_pyproject(tmp_path):
    """`pyproject.toml` with only `[tool.*]` tables is not a Python project."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 88\n", encoding="UTF-8"
    )
    assert is_python_project(tmp_path) is False


def test_is_python_project_false_for_missing_pyproject(tmp_path):
    """A directory with no `pyproject.toml` is not a Python project."""
    assert is_python_project(tmp_path) is False


def test_is_python_project_false_for_malformed_toml(tmp_path):
    """A `pyproject.toml` that fails to parse is not a Python project."""
    (tmp_path / "pyproject.toml").write_text("not = valid = toml\n", encoding="UTF-8")
    assert is_python_project(tmp_path) is False


def test_is_python_project_false_for_invalid_pep621(tmp_path):
    """A `[project]` table missing required PEP 621 fields is not Python.

    `[project]` with only `name` (no `version`, no `dynamic`) fails the
    `StandardMetadata.from_pyproject` validation, so the repo is not
    considered Python. Locks in the stricter PEP 621 semantics.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "incomplete"\n', encoding="UTF-8"
    )
    assert is_python_project(tmp_path) is False


def test_is_python_project_accepts_preloaded_data():
    """`is_python_project` accepts a pre-parsed `pyproject.toml` dict."""
    data = {"project": {"name": "preloaded", "version": "0.1.0"}}
    assert is_python_project(pyproject_data=data) is True


PEP621_TABLE = '[project]\nname = "melon-stand"\nversion = "0.1.0"\n'
"""Minimal valid PEP 621 header the `is_python_package` cases build on."""


@pytest.mark.parametrize(
    ("tool_table", "expected"),
    [
        # No [tool.uv] table at all: a plain package.
        ("", True),
        # A [tool.uv] table that says nothing about packaging.
        ('[tool.uv]\nrequired-version = ">=0.11.15"\n', True),
        # The explicit opt-in is still a package.
        ("[tool.uv]\npackage = true\n", True),
        # The virtual-project opt-out.
        ("[tool.uv]\npackage = false\n", False),
        # A sibling tool's `package` key must not be mistaken for uv's.
        ("[tool.hatch]\npackage = false\n", True),
    ],
)
def test_is_python_package_reads_uv_opt_out(tmp_path, tool_table: str, expected: bool):
    """Only `[tool.uv] package = false` demotes a PEP 621 project."""
    (tmp_path / "pyproject.toml").write_text(
        PEP621_TABLE + tool_table, encoding="UTF-8"
    )
    assert is_python_project(tmp_path) is True
    assert is_python_package(tmp_path) is expected


@pytest.mark.parametrize(
    "content",
    [
        # Not a Python project at all.
        "[tool.ruff]\nline-length = 88\n",
        # Invalid PEP 621, even though uv would build it.
        '[project]\nname = "incomplete"\n\n[tool.uv]\npackage = true\n',
        # Malformed TOML.
        "not = valid = toml\n",
    ],
)
def test_is_python_package_implies_is_python_project(tmp_path, content: str):
    """A non-Python repo is never a package, whatever `[tool.uv]` claims.

    Locks in the narrowing relation the `PACKAGE_ONLY` scope depends on:
    `is_python_package` can never be `True` where `is_python_project` is
    `False`, so a `PACKAGE_ONLY` entry can never outlive a `PYTHON_ONLY` one.
    """
    (tmp_path / "pyproject.toml").write_text(content, encoding="UTF-8")
    assert is_python_project(tmp_path) is False
    assert is_python_package(tmp_path) is False


def test_is_python_package_false_for_missing_pyproject(tmp_path):
    """A directory with no `pyproject.toml` builds no package."""
    assert is_python_package(tmp_path) is False


def test_is_python_package_accepts_preloaded_data():
    """`is_python_package` accepts a pre-parsed `pyproject.toml` dict."""
    data = {
        "project": {"name": "preloaded", "version": "0.1.0"},
        "tool": {"uv": {"package": False}},
    }
    assert is_python_package(pyproject_data=data) is False


def test_read_pyproject_toml_survives_file_vanishing_after_check(tmp_path, monkeypatch):
    """A file disappearing between the existence check and the read yields `{}`.

    The default `project_root` is relative, so the file can be deleted, or the
    working directory itself removed, in the window between the two. Both land
    on the missing-file outcome the docstring already promises, rather than
    escaping as `OSError`.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "papaya"\n', encoding="UTF-8")

    def vanish(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "pyproject.toml")

    monkeypatch.setattr("pathlib.Path.read_text", vanish)
    assert read_pyproject_toml(tmp_path) == {}


def test_dependency_group_names_reads_the_declared_groups(tmp_path):
    """Groups come off `[dependency-groups]`, sorted."""
    (tmp_path / "pyproject.toml").write_text(
        dedent("""\
            [dependency-groups]
            test = ["pytest"]
            docs = ["sphinx"]
            """),
        encoding="UTF-8",
    )
    assert dependency_group_names(tmp_path) == ("docs", "test")


def test_extra_names_reads_the_declared_extras(tmp_path):
    """Extras come off `[project.optional-dependencies]`, sorted."""
    (tmp_path / "pyproject.toml").write_text(
        dedent("""\
            [project]
            name = "papaya"
            version = "1.0.0"

            [project.optional-dependencies]
            xml = ["lxml"]
            csv = ["pandas"]
            """),
        encoding="UTF-8",
    )
    assert extra_names(tmp_path) == ("csv", "xml")


def test_group_and_extra_names_are_empty_without_a_pyproject(tmp_path):
    """A directory holding no project declares neither."""
    assert dependency_group_names(tmp_path) == ()
    assert extra_names(tmp_path) == ()


def test_cached_toml_parses_once_per_version_of_the_file(tmp_path):
    """Reads share one parse until the bytes change."""
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "fig"\n', encoding="UTF-8")
    first = cached_toml(path)
    assert cached_toml(path) is first
    path.write_text('[project]\nname = "figs"\n', encoding="UTF-8")
    second = cached_toml(path)
    assert second is not first
    assert second["project"]["name"] == "figs"


def test_cached_toml_raises_on_a_missing_or_broken_file(tmp_path):
    """The cache holds parses only: each caller decides what a failure means."""
    with pytest.raises(OSError):
        cached_toml(tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("[project\n", encoding="UTF-8")
    with pytest.raises(tomlrt.TOMLParseError):
        cached_toml(broken)


def test_read_pyproject_toml_and_the_lock_share_the_cache(tmp_path):
    """`pyproject.toml` and `uv.lock` read through the same parser."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fig"\n', encoding="UTF-8"
    )
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n", encoding="UTF-8")
    assert read_pyproject_toml(tmp_path) is cached_toml(tmp_path / "pyproject.toml")
    assert LockFile.load(lock).data is cached_toml(lock)
    assert read_pyproject_toml(tmp_path / "missing") == {}
    assert LockFile.load(tmp_path / "missing.lock").data == {}


def test_load_pyproject_doc_is_private_and_editable(tmp_path):
    """An edit to the editable parse never reaches the shared cached one."""
    path = tmp_path / "pyproject.toml"
    path.write_text("[tool.uv]\npackage = false\n", encoding="UTF-8")
    doc = load_pyproject_doc(path)
    assert doc is not cached_toml(path)
    uv_table(doc)["package"] = True
    assert cached_toml(path)["tool"]["uv"]["package"] is False
    assert tomlrt.dumps(doc) == "[tool.uv]\npackage = true\n"


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        pytest.param("[tool.uv]\npackage = false\n", {"package": False}, id="table"),
        pytest.param("[tool.ruff]\nfix = true\n", {}, id="no-uv-table"),
    ),
)
def test_uv_table(tmp_path, content, expected):
    """The `[tool.uv]` table reads empty when the project declares none."""
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="UTF-8")
    assert dict(uv_table(load_pyproject_doc(path))) == expected


PYPROJECT_BUMPVERSION = '[tool.bumpversion]\ncurrent_version = "4.5.6"\n'
"""A `pyproject.toml` declaring its version in the bump-my-version table."""


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {
                ".bumpversion.toml": 'current_version = "1.2.3"\n',
                "pyproject.toml": PYPROJECT_BUMPVERSION,
            },
            "1.2.3",
            id="bumpversion-file-first",
        ),
        pytest.param(
            {"pyproject.toml": PYPROJECT_BUMPVERSION}, "4.5.6", id="pyproject"
        ),
        pytest.param(
            {".bumpversion.toml": "[apple", "pyproject.toml": PYPROJECT_BUMPVERSION},
            "4.5.6",
            id="unparsable-file-skipped",
        ),
        pytest.param(
            {".bumpversion.toml": "[orchard]\nrows = 3\n"}, None, id="no-version"
        ),
        pytest.param(
            {"pyproject.toml": '[tool]\nbumpversion = "flat"\n'},
            None,
            id="not-a-table",
        ),
        pytest.param({}, None, id="no-file"),
    ],
)
def test_get_current_version(tmp_path, files, expected):
    """The first bump-my-version source declaring a version wins."""
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="UTF-8")
    assert get_current_version(tmp_path) == expected


def test_get_current_version_reads_the_working_directory(tmp_path, monkeypatch):
    """With no root given, the version comes from the working directory."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_BUMPVERSION, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    assert get_current_version() == "4.5.6"
