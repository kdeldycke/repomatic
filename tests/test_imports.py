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

"""Guard repomatic against importing click_extra's non-public API.

click_extra is both a runtime dependency of repomatic and the framework whose
release pipeline runs the *pinned* repomatic to build its metadata and release
notes. Reaching for an underscore-prefixed name or a private submodule couples
repomatic to click_extra internals that can be renamed without notice; the
rename then breaks the pinned repomatic from inside click_extra's own release:
the ``metadata`` step crashes after the package is already on PyPI, leaving it
untagged with no GitHub release. So repomatic imports only click_extra's public
API, promoting any needed helper to public in click_extra first. The companion
release-ordering rule (ship the fixed repomatic and bump click_extra's pin
before releasing click_extra) lives in ``claude.md`` § "click_extra is both a
dependency and a release consumer".
"""

from __future__ import annotations

import ast

import pytest

from tests.conftest import PACKAGE_FILES, PROJECT_ROOT


def _private_click_extra_imports(tree: ast.Module) -> list[str]:
    """Return the click_extra imports that reach for a private name or module.

    Flags ``from click_extra... import _name`` (private symbol) and any module
    path with an underscore-prefixed component below the top-level
    ``click_extra`` package (private submodule), for both ``import`` and
    ``from ... import`` statements.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            if parts[0] != "click_extra":
                continue
            if any(part.startswith("_") for part in parts[1:]):
                violations.append(f"from {node.module} import ...")
            violations.extend(
                f"from {node.module} import {alias.name}"
                for alias in node.names
                if alias.name.startswith("_")
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "click_extra" and any(
                    part.startswith("_") for part in parts[1:]
                ):
                    violations.append(f"import {alias.name}")
    return violations


@pytest.mark.once
def test_source_tree_discovered() -> None:
    """The glob must find modules, so the scan never passes vacuously."""
    assert PACKAGE_FILES


@pytest.mark.once
def test_detector_flags_private_imports() -> None:
    """The detector must actually catch the patterns it guards against."""
    sample = (
        "from click_extra.config import _make_schema_callable\n"
        "from click_extra import public_name\n"
        "from click_extra._internal import helper\n"
        "import click_extra._private\n"
        "from other_pkg import _ignored\n"
    )
    assert set(_private_click_extra_imports(ast.parse(sample))) == {
        "from click_extra.config import _make_schema_callable",
        "from click_extra._internal import ...",
        "import click_extra._private",
    }


@pytest.mark.once
@pytest.mark.parametrize(
    "source_file",
    PACKAGE_FILES,
    ids=[str(p.relative_to(PROJECT_ROOT)) for p in PACKAGE_FILES],
)
def test_no_private_click_extra_imports(source_file) -> None:
    """repomatic must import only click_extra's public API."""
    tree = ast.parse(source_file.read_text(encoding="UTF-8"))
    violations = _private_click_extra_imports(tree)
    assert not violations, (
        f"{source_file.relative_to(PROJECT_ROOT)} imports click_extra "
        f"private API: {', '.join(violations)}"
    )
