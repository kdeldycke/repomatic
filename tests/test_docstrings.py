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

"""Guard docstrings against a brace placeholder the MyST converter eats.

`click_extra.sphinx.myst_docstrings` rewrites MyST docstrings to reST at build
time, and its cross-reference pattern reads a brace placeholder followed by a
backtick as the opening of a role. A placeholder sitting against the *closing*
backtick of an inline code span therefore opens a role instead of closing the
span: the converter puts a role marker of the same name where the placeholder
was, then swallows the prose up to the next backtick in the docstring. Both
code-span boundaries move, and a sentence of documentation disappears into a
literal.

Nothing reports it. What comes out is a well-formed role Sphinx has no opinion
about, so the build stays warning-free and the corruption surfaces only to
whoever reads the rendered page. That silence is what makes this worth a
conformance test rather than a comment.

Doubling the backticks around the code span is the converter's documented
opt-out, and the fix every occurrence in the tree uses today.

A genuine role converts exactly the way an eaten placeholder does, so the two
are told apart by name: a marker named in `KNOWN_ROLES` is a role this project
may legitimately write, anything else is a placeholder that got eaten. The
heuristic is deliberately precision-first, and blind by construction to a
placeholder named after a real role. A missed occurrence costs one corrupted
paragraph; a false positive would fail the suite on a legitimate
cross-reference.

The check drives the real converter rather than a local copy of its pattern. A
copy would report shapes the converter leaves alone: the conversion is a
multi-pass pipeline that masks reST literals and protected spans before the
cross-reference pattern ever sees the text, so the pattern on its own is not
what decides the outcome. Calling upstream also means this test follows it
instead of drifting from it.

```{caution}
That converter arrives with `click-extra[sphinx]`, which only the `docs`
dependency group pulls in, so the module skips wherever Sphinx is absent. Add
Sphinx to the `test` group to turn this from a local check into a CI gate.
```
"""

from __future__ import annotations

import ast
import re

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

myst_to_rst = pytest.importorskip(
    "click_extra.sphinx.myst_docstrings",
    reason="needs click-extra[sphinx], pulled in by the docs dependency group",
    exc_type=ImportError,
).myst_to_rst
"""Convert a list of docstring lines from MyST to reST, in place.

Imported dynamically so a test environment without Sphinx skips this module
instead of failing to collect the whole suite. `exc_type` has to widen to
`ImportError`: `click_extra.sphinx` catches the missing Sphinx itself and
re-raises a plain `ImportError` carrying an install hint, which the default
`ModuleNotFoundError` would let through as a collection error.
"""

KNOWN_ROLES = frozenset((
    "abbr",
    "attr",
    "class",
    "command",
    "data",
    "doc",
    "download",
    "envvar",
    "exc",
    "file",
    "func",
    "guilabel",
    "kbd",
    "keyword",
    "manpage",
    "meth",
    "mod",
    "obj",
    "octicon",
    "option",
    "program",
    "py",
    "ref",
    "samp",
    "term",
))
"""Role names a docstring may legitimately carry.

A converted marker named here is read as an intentional cross-reference. A
placeholder unlucky enough to share one of these names is read the same way and
passes unflagged, which is the price of never failing on a real role.
"""

PLACEHOLDER_RE = re.compile(r"\{([\w-]+)\}")
"""Match a brace placeholder, the shape the converter mistakes for a role."""


def _myst_to_rst(docstring: str) -> str:
    """Convert a docstring the way Sphinx does at build time."""
    lines = docstring.split("\n")
    myst_to_rst(lines)
    return "\n".join(lines)


def _eaten_placeholders(docstring: str) -> set[str]:
    """Return the placeholders the conversion replaced with a role marker.

    A placeholder counts as eaten when its name is not one this project writes
    as a role, the converted text carries a marker of that name, and the
    literal placeholder is gone from it.
    """
    converted = _myst_to_rst(docstring)
    return {
        match.group(0)
        for match in PLACEHOLDER_RE.finditer(docstring)
        if match.group(1) not in KNOWN_ROLES
        and f":{match.group(1)}:" in converted
        and match.group(0) not in converted
    }


def _attribute_docstrings(tree: ast.Module) -> Iterator[tuple[int, str, str]]:
    """Yield `(line, owner, text)` for each attribute docstring in *tree*.

    An attribute docstring is the bare string literal following an assignment.
    `ast.get_docstring` cannot see one, yet `claude.md` makes it the convention
    for documenting a dataclass field and `automodule ... :undoc-members:`
    renders it, so it mangles exactly like any other docstring.
    """
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        yield from attribute_docstrings(body)


def _mangled_docstrings(tree: ast.Module) -> list[str]:
    """Return one description per docstring the conversion corrupts."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        docstring = ast.get_docstring(node, clean=False)
        # Skip the conversion for the overwhelming majority of docstrings, which
        # carry no brace at all.
        if not docstring or "{" not in docstring:
            continue
        if eaten := _eaten_placeholders(docstring):
            # A module carries no line number of its own.
            line = getattr(node, "lineno", 1)
            name = getattr(node, "name", "<module>")
            violations.append(f"line {line}: {name}: {', '.join(sorted(eaten))}")

    for line, owner, docstring in _attribute_docstrings(tree):
        if "{" not in docstring:
            continue
        if eaten := _eaten_placeholders(docstring):
            violations.append(f"line {line}: {owner}: {', '.join(sorted(eaten))}")

    return sorted(violations)


def test_package_discovered() -> None:
    """The scan actually walks the package, so a green result means something."""
    assert len(PACKAGE_FILES) > 40
    assert PACKAGE_DIR / "cli" / "main.py" in PACKAGE_FILES


def test_detector_flags_eaten_placeholder() -> None:
    """The detector catches the corrupted span, and clears the safe shapes."""
    tree = ast.parse(
        '"""The `harvest/{orchard}` path holds `mango` crops."""\n'
        "class Doubled:\n"
        '    """The ``harvest/{orchard}`` path holds `mango` crops."""\n'
        "def roles():\n"
        '    """See {func}`pick_fruit` and {class}`Orchard` for details."""\n'
        "async def prose():\n"
        '    """A {orchard} placeholder in prose, and `mango` too."""\n'
    )
    violations = _mangled_docstrings(tree)
    # Only the single-backtick span is corrupted. The doubled span opts out, the
    # two genuine roles are named in KNOWN_ROLES, and the unquoted placeholder
    # never touches a backtick.
    assert violations == ["line 1: <module>: {orchard}"], violations


@pytest.mark.parametrize(
    "source_file",
    PACKAGE_FILES,
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_no_mangled_docstrings(source_file) -> None:
    """No docstring loses a placeholder to the MyST-to-reST conversion."""
    tree = ast.parse(source_file.read_text(encoding="UTF-8"))
    violations = _mangled_docstrings(tree)
    assert not violations, (
        f"{source_file.relative_to(PROJECT_ROOT)} has a brace placeholder against"
        f" the closing backtick of an inline code span: {'; '.join(violations)}."
        " The MyST-to-reST conversion turns it into a role and swallows the prose"
        " that follows. Double the backticks around the code span to opt out."
    )
