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
"""Tests for bundled awesome-template data and sync logic."""

from __future__ import annotations

from importlib.resources import files

from repomatic.init_project import _copy_template_tree

TEMPLATE_ROOT = files("repomatic.data").joinpath("awesome_template")

# Key files that must be present in the bundled template.
EXPECTED_FILES = {
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/new-link.yaml",
    ".github/code-of-conduct.md",
    ".github/contributing.md",
    ".github/contributing.zh.md",
    ".github/funding.yml",
    ".github/pull_request_template.md",
    "license",
}


def _collect_files(root, prefix=""):
    """Recursively collect file paths from a traversable resource tree."""
    result = set()
    for entry in root.iterdir():
        name = f"{prefix}{entry.name}" if prefix else entry.name
        if entry.name in ("__init__.py", "__pycache__"):
            continue
        if entry.is_dir():
            result |= _collect_files(entry, f"{name}/")
        else:
            result.add(name)
    return result


def test_bundled_files_exist():
    """All expected awesome-template files are bundled in the package."""
    actual = _collect_files(TEMPLATE_ROOT)
    assert actual == EXPECTED_FILES


def test_copy_template_tree(tmp_path):
    """_copy_template_tree copies all files and skips __init__.py."""
    created, updated = _copy_template_tree(TEMPLATE_ROOT, tmp_path)
    assert len(created) == len(EXPECTED_FILES)
    assert updated == []

    # Verify key files exist at the expected paths.
    assert (tmp_path / "license").is_file()
    assert (tmp_path / ".github" / "contributing.md").is_file()
    assert (tmp_path / ".github" / "ISSUE_TEMPLATE" / "new-link.yaml").is_file()

    # __init__.py must not be copied.
    assert not (tmp_path / "__init__.py").exists()

    # pyproject.toml is no longer in the bundle (lychee is a ToolConfigComponent).
    assert not (tmp_path / "pyproject.toml").exists()

    # Second run is a no-op: files already exist and are identical.
    created2, updated2 = _copy_template_tree(TEMPLATE_ROOT, tmp_path)
    assert created2 == []
    assert updated2 == []


def test_copy_template_tree_creates_directories(tmp_path):
    """_copy_template_tree creates parent directories as needed."""
    dest = tmp_path / "nested" / "deep"
    created, _updated = _copy_template_tree(TEMPLATE_ROOT, dest)
    assert len(created) == len(EXPECTED_FILES)
    assert (dest / ".github" / "ISSUE_TEMPLATE" / "new-link.yaml").is_file()


def test_copied_files_contain_template_slug(tmp_path):
    """Copied .github/ markdown files contain the template slug for rewriting."""
    _copy_template_tree(TEMPLATE_ROOT, tmp_path)
    pr_template = tmp_path / ".github" / "pull_request_template.md"
    content = pr_template.read_text(encoding="UTF-8")
    assert "kdeldycke/awesome-template" in content


def test_awesome_template_has_no_pyproject(tmp_path):
    """awesome-template bundle does not contain pyproject.toml.

    The lychee config is registered as a ToolConfigComponent with
    AWESOME_ONLY scope, so it goes through the standard merge path
    in _init_tool_configs instead of being blindly overwritten by
    _copy_template_tree.
    """
    _copy_template_tree(TEMPLATE_ROOT, tmp_path)
    assert not (tmp_path / "pyproject.toml").exists()
