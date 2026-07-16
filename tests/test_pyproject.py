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

from repomatic.pyproject import get_project_name, is_python_project


def test_get_project_name_from_cwd(tmp_path, monkeypatch):
    """Test that get_project_name reads from pyproject.toml in CWD."""
    pyproject_content = """\
[project]
name = "my-package"
version = "1.0.0"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert get_project_name() == "my-package"


def test_get_project_name_missing_pyproject(tmp_path, monkeypatch):
    """Test that get_project_name returns None when no pyproject.toml."""
    monkeypatch.chdir(tmp_path)
    assert get_project_name() is None


def test_get_project_name_no_project_section(tmp_path, monkeypatch):
    """Test that get_project_name returns None when no [project] section."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
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
