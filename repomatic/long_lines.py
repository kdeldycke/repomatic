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
"""Wrap the comment and docstring lines that run past the line length.

Ruff's formatter wraps code, and keeps the line breaks of prose: a comment, or a
paragraph of a docstring, stays as long as it was written
([astral-sh/ruff#7414](https://github.com/astral-sh/ruff/issues/7414)), and `E501`
then fails `lint-python` on it.

{func}`fix_long_lines` pushes words down. An overlong line keeps the words that
fit, and the rest open the next line of its paragraph. That line can overflow in
turn, so the push goes on down the paragraph, and its last line gets a new one
below it. The lines above stay as they are and no other line break moves, so the
diff holds only the lines that had to change.

```{note}
A line that this cannot shorten is left for a human to reword: a code line, a
string literal, a trailing comment, a pragma comment, the summary line of a
docstring, which `D205` holds to one line, and a line of a code block, a doctest
or a table in a docstring. So are the two kinds of line `E501` itself ignores: a
single word, and a line that ends with a URL starting inside the limit.
```
"""

from __future__ import annotations

import ast
import io
import logging
import re
import tokenize
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .file_inventory import FileInventory
from .file_ops import write_if_changed
from .pyproject import cached_toml, read_pyproject_toml

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

DEFAULT_LINE_LENGTH = 88
"""The line length `E501` checks when no Ruff configuration sets one."""

PYTHON_SUFFIXES = frozenset({".py", ".pyi", ".pyw"})
"""The sources this rewrites: notebooks and Cython files do not parse as Python."""

RUFF_CONFIG_FILES = (".ruff.toml", "ruff.toml")
"""The Ruff configuration files that win over `[tool.ruff]`, in Ruff's order."""

_COMMENT_HEAD = re.compile(r" *#+ ")
"""The indentation and hash marks of a comment line, with the space after them.

A comment with no space after its hash marks, like `#:` or `#!`, never matches.
"""

_PRAGMA = re.compile(
    r"#+ *(?:noqa|type:|pragma|fmt:|isort:|pyright:|mypy:|ruff:|-\*-)",
    re.IGNORECASE,
)
"""A comment that a tool reads, whose words must stay on their line."""

_LIST_ITEM = re.compile(r"(?:[-*+]|\d+[.)]) +")
"""The marker of a list item, which opens a paragraph of its own."""

_FIELD = re.compile(r":[^:\s][^:]*: +")
"""The name of a reST field, like `:param name:`, which opens a paragraph too."""

FIELD_HANG = 4
"""How far the next lines of a reST field indent past the field name."""

_FENCE = re.compile(r"`{3,}|~{3,}")
"""The line that opens or closes a fenced block."""

_STOP = ('"""', "'''", ">>>", "...", "|")
"""Text that no paragraph runs into: closing quotes, a doctest and a table row."""


@dataclass
class _Line:
    """One line of a source file, and how the wrap may rewrite it."""

    text: str
    """The line, without its line ending."""

    group: tuple[str, int] | None = None
    """The comment block or the docstring of the line, or `None` for a line that
    the wrap never rewrites."""

    lead: str = ""
    """The start of the line that stays in place: indentation, the hash marks of a
    comment and the marker of a list item."""

    hang: str = ""
    """The start of the next lines of the paragraph: the indentation and the hash
    marks, then spaces up to the column where their words start."""

    def continues(self, previous: _Line) -> bool:
        """Tell whether this line goes on with the paragraph of *previous*."""
        return (
            self.group is not None
            and self.group == previous.group
            and self.lead == previous.hang
            and self.hang == previous.hang
        )


def _width(text: str) -> int:
    """Measure *text* as `E501` does: a wide East Asian character counts twice."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def is_overlong(line: str, limit: int) -> bool:
    """Tell whether `E501` reports *line* at *limit*.

    `E501` skips a line that no wrap can shorten: one that holds a single word,
    a `#` before it included, and one that ends with a URL starting inside the
    limit.

    :param line: The line, without its line ending.
    :param limit: The longest width a line may take.
    :return: Whether the line is too long and could be shorter.
    """
    width = _width(line)
    if width <= limit:
        return False
    chunks = line.split()
    if len(chunks) < 2 or (chunks[0] == "#" and len(chunks) == 2):
        return False
    return not ("://" in chunks[-1] and width - _width(chunks[-1]) <= limit)


def _mark(
    line: _Line, group: tuple[str, int], start: int, lead: int, hang: int
) -> None:
    """Give *line* its group, the part that stays in place and its hang.

    :param start: The column where the text of the paragraph starts, after the
        indentation and the hash marks.
    :param lead: The column where the words that may move start, after a list
        marker or a field name.
    :param hang: The column where the words of the next lines start.
    """
    line.group = group
    line.lead = line.text[:lead]
    line.hang = line.text[:start] + " " * (hang - start)


def _hangs_at(text: str, start: int, hang: int) -> bool:
    """Tell whether *text* holds only spaces from *start* to *hang*, then a word."""
    return len(text) > hang and not text[start:hang].strip() and text[hang] != " "


def _mark_comments(source: str, lines: list[_Line]) -> None:
    """Mark the full-line comments, block by block.

    A trailing comment shares its line with code, and a pragma comment holds words
    that a tool reads: neither is rewritten. Inside a block, a line indented past
    its hash marks is rewritten only as the next line of a list item. Any other
    such line is a sample, like a command or a table.
    """
    columns: dict[int, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        row, column = token.start
        before = lines[row - 1].text[:column]
        if before.strip() or "\t" in before or token.string.startswith("#!"):
            continue
        if not _PRAGMA.match(token.string):
            columns[row - 1] = column

    group: tuple[str, int] = ("comment", 0)
    hang: int | None = None
    for row in sorted(columns):
        if columns.get(row - 1) != columns[row]:
            group, hang = ("comment", row), None
        line = lines[row]
        head = _COMMENT_HEAD.match(line.text)
        if head is None or not line.text[head.end() :].strip():
            hang = None
            continue
        start = head.end()
        item = _LIST_ITEM.match(line.text, start)
        if item:
            hang = item.end()
            _mark(line, group, start, item.end(), hang)
        elif line.text[start] != " ":
            hang = None
            _mark(line, group, start, start, start)
        elif hang is not None and _hangs_at(line.text, start, hang):
            _mark(line, group, hang, hang, hang)


def _docstring_spans(tree: ast.AST) -> Iterator[tuple[int, int]]:
    """Yield the first and last row of each string statement: the docstrings."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement in body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
                and statement.end_lineno is not None
            ):
                yield statement.lineno - 1, statement.end_lineno - 1


def _mark_docstring(lines: list[_Line], first: int, last: int) -> None:
    """Mark the rows of one docstring that the wrap may rewrite.

    The summary is left out, and so is every block that a wrap would break: a
    fenced block, a block indented after a blank line, a doctest and a table. A
    line indented past the text is rewritten only as the next line of a list item
    or of a reST field, which indents {data}`FIELD_HANG` columns past its name.
    """
    opening = lines[first].text.strip().lstrip("rRbBuUfF")
    summary = first if opening.strip("\"'") else first + 1
    rows = range(summary + 1, last + 1)
    indents = [
        len(lines[row].text) - len(lines[row].text.lstrip(" "))
        for row in rows
        if lines[row].text.strip()
    ]
    if not indents:
        return
    base = min(indents)
    group = ("docstring", first)
    fenced = blocked = False
    blank_before = True
    hang: int | None = None
    for row in rows:
        line = lines[row]
        text = line.text.strip()
        if not text:
            blank_before, hang = True, None
            continue
        indent = len(line.text) - len(line.text.lstrip(" "))
        after_blank, blank_before = blank_before, False
        if _FENCE.match(text):
            fenced = not fenced
            continue
        if indent > base and after_blank:
            blocked = True
        elif indent == base:
            blocked = False
        if fenced or blocked or text.startswith(_STOP) or "\t" in line.text:
            continue
        item = _LIST_ITEM.match(line.text, indent)
        field = _FIELD.match(line.text, indent)
        if item:
            hang = item.end()
            _mark(line, group, indent, item.end(), hang)
        elif field:
            hang = indent + FIELD_HANG
            _mark(line, group, indent, field.end(), hang)
        elif indent == base:
            hang = None
            _mark(line, group, indent, indent, indent)
        elif indent == hang:
            _mark(line, group, indent, indent, indent)


def _push_down(lines: list[_Line], row: int, limit: int) -> int:
    """Shorten the line at *row*, and each line its words then overflow.

    :return: How many lines got shorter.
    """
    shortened = 0
    while row < len(lines) and is_overlong(lines[row].text, limit):
        line = lines[row]
        words = line.text[len(line.lead) :].split(" ")
        moved: list[str] = []
        while len(words) > 1 and _width(line.lead + " ".join(words)) > limit:
            moved.insert(0, words.pop())
        if not moved:
            break
        line.text = line.lead + " ".join(words)
        shortened += 1
        following = lines[row + 1] if row + 1 < len(lines) else None
        if following is not None and following.continues(line):
            rest = following.text[len(line.hang) :]
            following.text = f"{line.hang}{' '.join(moved)} {rest}"
        else:
            lines.insert(
                row + 1,
                _Line(
                    f"{line.hang}{' '.join(moved)}",
                    group=line.group,
                    lead=line.hang,
                    hang=line.hang,
                ),
            )
        row += 1
    return shortened


def wrap_source(source: str, limit: int) -> tuple[str, int]:
    """Wrap the overlong comment and docstring lines of one Python source.

    :param source: The text of the file.
    :param limit: The longest width a line may take.
    :return: The new text, and how many lines got shorter.
    :raises SyntaxError: when *source* does not parse as Python.
    """
    lines = [_Line(text) for text in source.split("\n")]
    _mark_comments(source, lines)
    for first, last in _docstring_spans(ast.parse(source)):
        _mark_docstring(lines, first, last)
    shortened = 0
    # Bottom up: a push only ever inserts a line below the row it starts from.
    for row in reversed(range(len(lines))):
        if lines[row].group is not None and is_overlong(lines[row].text, limit):
            shortened += _push_down(lines, row, limit)
    return "\n".join(line.text for line in lines), shortened


def ruff_line_length(project_root: Path | None = None) -> int:
    """Read the line length that Ruff checks in *project_root*.

    Ruff reads `.ruff.toml`, then `ruff.toml`, then the `[tool.ruff]` table of
    `pyproject.toml`, and stops at the first one it finds.

    :param project_root: The directory to read. Defaults to the current one.
    :return: The `line-length` that configuration sets, or
        {data}`DEFAULT_LINE_LENGTH`.
    """
    root = Path() if project_root is None else project_root
    for name in RUFF_CONFIG_FILES:
        path = root / name
        if path.is_file():
            return int(cached_toml(path).get("line-length", DEFAULT_LINE_LENGTH))
    ruff = read_pyproject_toml(root).get("tool", {}).get("ruff", {})
    return int(ruff.get("line-length", DEFAULT_LINE_LENGTH))


def fix_long_lines(
    paths: Iterable[Path] | None = None, line_length: int | None = None
) -> dict[Path, int]:
    """Wrap the overlong comment and docstring lines of *paths*.

    A file that does not parse as Python, or that has Windows line endings, is
    left as it is, with a warning.

    :param paths: The files to rewrite. Defaults to the Python sources of the
        repository.
    :param line_length: The longest width a line may take. Defaults to
        {func}`ruff_line_length`.
    :return: Each file that changed, with how many of its lines got shorter.
    """
    limit = line_length or ruff_line_length()
    if paths is None:
        paths = (
            path
            for path in FileInventory().python_files
            if path.suffix in PYTHON_SUFFIXES
        )
    report: dict[Path, int] = {}
    for path in paths:
        # Read the line endings as they are, so a Windows file is found and left.
        with path.open(encoding="UTF-8", newline="") as handle:
            source = handle.read()
        if "\r" in source:
            logging.warning(f"Leaving {path} as it is: it has Windows line endings.")
            continue
        try:
            wrapped, shortened = wrap_source(source, limit)
        except (SyntaxError, ValueError, tokenize.TokenError) as exc:
            logging.warning(f"Leaving {path} as it is: it does not parse ({exc}).")
            continue
        if write_if_changed(path, wrapped, previous=source):
            report[path] = shortened
    return report
