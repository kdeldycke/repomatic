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

"""Guard every `{todo}` admonition against never reaching the published page.

`sphinx.ext.todo` collects the admonitions onto `docs/todolist.md`, which is
the project's inventory of what it owes. An item written where `autodoc` never
looks is absent from that inventory: the work stays undone and unlisted, and
the page reads as complete while under-reporting.

`autodoc` skips a name starting with an underscore, so a `{todo}` on a private
constant, function or class publishes nowhere. Two of them sat on the private
regexes behind the `mdformat` post-process shim in `repomatic.tool_registry`,
and the page carried 14 items while the tree held 16.

Nothing reports it. A `{todo}` renders exactly the same way whether or not its
owner is picked up, the build stays warning-free, and the only way to notice is
to count the page against the tree by hand. That silence is what earns this a
conformance test rather than a line in `claude.md`.

The check reads the owner from the syntax tree instead of importing anything,
so it runs everywhere the suite does. `tests/test_docstrings.py` covers the
same population for a different corruption, but drives the real MyST converter
and therefore skips wherever Sphinx is absent.
"""

from __future__ import annotations

import ast

import pytest

from tests.conftest import (
    PACKAGE_DIR,
    PACKAGE_FILES,
    PROJECT_ROOT,
    attribute_docstrings,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

TODO_MARKER = "```{todo}"
"""Opening fence of a todo admonition, as written throughout the package."""

CONF_PY = PROJECT_ROOT / "docs" / "conf.py"
"""Sphinx configuration, read for the `autodoc` options this check assumes."""


def _autodoc_default_options() -> dict[str, object]:
    """Return the `autodoc_default_options` mapping declared in `conf.py`.

    Parsed rather than imported: `conf.py` runs under Sphinx and pulls in the
    whole documentation stack, which the test environment does not carry.
    """
    tree = ast.parse(CONF_PY.read_text(encoding="UTF-8"))
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if "autodoc_default_options" in names:
            options = ast.literal_eval(node.value)
            assert isinstance(options, dict), options
            return options
    raise AssertionError(f"No autodoc_default_options found in {CONF_PY}.")


def _is_private(name: str) -> bool:
    """Whether *name* is one `autodoc` skips by default."""
    return name.startswith("_")


def _module_path(source_file: Path) -> str:
    """Return the dotted import path of *source_file*."""
    relative = source_file.relative_to(PACKAGE_DIR.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _todo_owners(tree: ast.Module, module: str) -> Iterator[tuple[int, str]]:
    """Yield `(line, dotted_owner)` for every todo admonition in *tree*.

    The owner carries its full nesting path, so a public method on a private
    class is reported under the private ancestor that actually hides it.
    """

    def visit(node: ast.AST, prefix: str) -> Iterator[tuple[int, str]]:
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            return
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            docstring = ast.get_docstring(node, clean=False)
            if docstring and TODO_MARKER in docstring:
                yield getattr(node, "lineno", 1), prefix
        for line, owner, text in attribute_docstrings(body):
            if TODO_MARKER in text:
                yield line, f"{prefix}.{owner}"
        # Only a class body nests further: `autodoc` never reaches a name
        # defined inside a function.
        if isinstance(node, (ast.Module, ast.ClassDef)):
            for statement in body:
                if isinstance(
                    statement,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    yield from visit(statement, f"{prefix}.{statement.name}")

    yield from visit(tree, module)


def _unpublished_todos(tree: ast.Module, module: str) -> list[str]:
    """Return one description per todo admonition `autodoc` never renders."""
    violations = []
    for line, owner in _todo_owners(tree, module):
        hidden = [part for part in owner.split(".") if _is_private(part)]
        if hidden:
            violations.append(f"line {line}: {owner} (hidden by {hidden[0]})")
    return sorted(violations)


def test_package_discovered() -> None:
    """The scan actually walks the package, so a green result means something."""
    assert len(PACKAGE_FILES) > 40
    assert PACKAGE_DIR / "tool_registry.py" in PACKAGE_FILES


def test_the_package_owes_something() -> None:
    """The tree carries todo admonitions at all, so the sweep has a subject.

    An empty population would pass every assertion below while proving nothing.
    """
    total = sum(
        1
        for source_file in PACKAGE_FILES
        for _ in _todo_owners(
            ast.parse(source_file.read_text(encoding="UTF-8")),
            _module_path(source_file),
        )
    )
    assert total >= 10, f"Only {total} todo admonitions found in the package."


def test_autodoc_skips_private_members() -> None:
    """Pin the premise the sweep below rests on.

    Turning `private-members` on would publish a private owner's docstring and
    make the sweep over-strict. It is off today, and this fails the moment that
    changes, rather than leaving the sibling test quietly wrong.
    """
    options = _autodoc_default_options()
    assert not options.get("private-members"), (
        "docs/conf.py now renders private members, so a `{todo}` on one does"
        " reach the todo list. Relax `_unpublished_todos` accordingly."
    )


def test_detector_flags_hidden_todo() -> None:
    """The detector catches each hidden owner, and clears the published ones."""
    tree = ast.parse(
        f'"""Module prose.\n\n{TODO_MARKER}\nPick the mangoes.\n```\n"""\n'
        "RIPENESS = 3\n"
        f'"""Public constant.\n\n{TODO_MARKER}\nWeigh the papayas.\n```\n"""\n'
        "_CRATE_SIZE = 12\n"
        f'"""Private constant.\n\n{TODO_MARKER}\nCount the crates.\n```\n"""\n'
        "class Orchard:\n"
        "    rows = 4\n"
        f'    """Public field.\n\n{TODO_MARKER}\nMeasure the rows.\n```\n"""\n'
        "class _Shed:\n"
        "    def sweep(self):\n"
        f'        """Public method on a private class.\n\n{TODO_MARKER}\n'
        'Sweep.\n```\n"""\n'
    )
    assert _unpublished_todos(tree, "harvest") == [
        "line 15: harvest._CRATE_SIZE (hidden by _CRATE_SIZE)",
        "line 30: harvest._Shed.sweep (hidden by _Shed)",
    ], _unpublished_todos(tree, "harvest")


@pytest.mark.parametrize(
    "source_file",
    PACKAGE_FILES,
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_every_todo_is_published(source_file) -> None:
    """Every todo admonition sits where `autodoc` renders it onto the page."""
    tree = ast.parse(source_file.read_text(encoding="UTF-8"))
    violations = _unpublished_todos(tree, _module_path(source_file))
    assert not violations, (
        f"{source_file.relative_to(PROJECT_ROOT)} carries a `{TODO_MARKER}`"
        " admonition on a private owner, so it never reaches"
        " https://repomatic.net/todolist:\n  " + "\n  ".join(violations) + "\n"
        "Move it to the public docstring that owns the concern, and leave a"
        " pointer beside the code."
    )
