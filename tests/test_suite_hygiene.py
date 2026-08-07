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

"""Guard the test suite against parametrizing over an unordered collection.

`pytest-xdist` distributes work by test ID, so every worker must collect the same
IDs in the same order. A `@pytest.mark.parametrize` fed a `set`, `frozenset`, or
`dict` iterates in an order that depends on the process's string-hash seed, so
each worker derives different IDs and the whole run aborts with "Different tests
were collected between gw0 and gw1" before a single test executes. The failure
names the workers rather than the parametrization, and the suite still passes
serially, which is what makes it worth a conformance test rather than a comment.

Wrapping the argument in `sorted()` fixes it, and is what every parametrization
in the suite does today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
"""Root of the test suite."""

TEST_FILES = sorted(TESTS_DIR.rglob("*.py"))
"""Every Python module in the test suite."""

UNORDERED_CALLS = frozenset({"set", "frozenset"})
"""Builtins whose result has no stable iteration order across processes."""


def _unordered_names(tree: ast.Module) -> dict[str, str]:
    """Map module-level names bound to an unordered collection to its kind.

    Only module-level bindings are tracked: a parametrization can reference
    nothing narrower, since decorators are evaluated at import time.
    """
    kinds: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        kind = ""
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in UNORDERED_CALLS
        ):
            kind = f"{value.func.id}() call"
        elif isinstance(value, ast.Set):
            kind = "set literal"
        elif isinstance(value, ast.SetComp):
            kind = "set comprehension"
        elif isinstance(value, ast.DictComp):
            kind = "dict comprehension"
        elif isinstance(value, ast.Dict):
            kind = "dict literal"
        if kind:
            for target in targets:
                if isinstance(target, ast.Name):
                    kinds[target.id] = kind
    return kinds


def _unordered_parametrize(tree: ast.Module) -> list[str]:
    """Return one description per parametrization fed an unordered collection."""
    unordered = _unordered_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not ast.unparse(node.func).endswith("parametrize"):
            continue
        # parametrize(argnames, argvalues): only the second argument is iterated
        # into test IDs.
        if len(node.args) < 2:
            continue
        argvalues = node.args[1]
        kind = ""
        if isinstance(argvalues, ast.Name) and argvalues.id in unordered:
            kind = f"{argvalues.id} is a {unordered[argvalues.id]}"
        elif isinstance(argvalues, (ast.Set, ast.SetComp)):
            kind = "inline set"
        elif isinstance(argvalues, (ast.Dict, ast.DictComp)):
            kind = "inline dict"
        if kind:
            violations.append(f"line {node.lineno}: {kind}")
    return violations


def test_test_suite_discovered() -> None:
    """The scan actually walks the suite, so a green result means something."""
    assert len(TEST_FILES) > 40
    assert TESTS_DIR / "conftest.py" in TEST_FILES


def test_detector_flags_unordered_parametrize() -> None:
    """The detector catches each unordered shape, and clears the sorted ones."""
    tree = ast.parse(
        "FRUITS = frozenset({'papaya', 'mango'})\n"
        "CITIES = {'Lisbon', 'Oslo'}\n"
        "WEIGHTS = {'papaya': 2, 'mango': 1}\n"
        "SORTED = sorted(FRUITS)\n"
        "@pytest.mark.parametrize('fruit', FRUITS)\n"
        "@pytest.mark.parametrize('city', CITIES)\n"
        "@pytest.mark.parametrize('fruit', WEIGHTS)\n"
        "@pytest.mark.parametrize('fruit', {'papaya', 'mango'})\n"
        "@pytest.mark.parametrize('fruit', SORTED)\n"
        "@pytest.mark.parametrize('fruit', sorted(FRUITS))\n"
        "@pytest.mark.parametrize('fruit', ['papaya', 'mango'])\n"
        "def test_harvest(fruit, city):\n"
        "    pass\n"
    )
    violations = _unordered_parametrize(tree)
    # The three sorted or listed parametrizations are silent; the four unordered
    # ones each name the shape that made them unordered.
    report = "\n".join(violations)
    assert len(violations) == 4, report
    assert "FRUITS is a frozenset() call" in report
    assert "CITIES is a set literal" in report
    assert "WEIGHTS is a dict literal" in report
    assert "inline set" in report


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_no_unordered_parametrize(test_file: Path) -> None:
    """No parametrization iterates a collection with an unstable order."""
    tree = ast.parse(test_file.read_text(encoding="utf-8"))
    violations = _unordered_parametrize(tree)
    assert not violations, (
        f"{test_file.relative_to(TESTS_DIR.parent)} parametrizes over an unordered"
        f" collection, which breaks xdist collection: {'; '.join(violations)}."
        " Wrap the argument in sorted()."
    )
