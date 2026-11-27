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
"""Wrap the comment and docstring lines that Ruff reports as too long.

Ruff's formatter wraps code, and keeps the line breaks of prose: a comment, or a
paragraph of a docstring, stays as long as it was written
([astral-sh/ruff#7414](https://github.com/astral-sh/ruff/issues/7414)), and `E501`
then fails `lint-python` on it.

autopep8 `2.3.2` shortens a comment line only under `--aggressive`. Even then, it
wraps only the last line of a comment block, to at most 72 characters of text.

Ruff picks the lines. {func}`fix_long_lines` runs the pinned Ruff with the rule
selection of the project, plus {data}`RUFF_RULES`, and wraps only the comment and
docstring lines that `E501` or `W505` reports, each to the limit of its message. So
every setting that Ruff reads applies: `max-line-length`, `max-doc-length`,
`extend`, nested configuration files, `exclude`, `per-file-ignores`, each `noqa`
form, `# ruff: disable[...]` ranges, the task-comment options, tab stops and the
width of Unicode characters. A project whose rule selection leaves `E501` out gets
no wrap, unless `--line-length` adds it.

An overlong line keeps the words that fit, and the other words move down:

- In a comment, they open a new line below, with the same indentation, hash marks
  and list hang. The next comment line stays as it is, so two comment lines never
  merge into one.
- In a docstring, they open the next line of the paragraph, because reST and
  Markdown join the lines of a paragraph. That line can overflow in turn, so the
  push goes on down the paragraph, and its last line gets a new one below it.

Ruff then checks each changed file again. A comment block or a docstring whose
wrap gives a rule a new finding gets its old lines back, with a warning. So does a
docstring whose wrap would change its string, like a named character escape that
a break would split. The other blocks of the file keep their wrap.

```{note}
These lines stay as they are. The wrap shortens none of them, and moves no word
into them:

- Machine-read comments: pragmas, encoding declarations, modelines, spell-checker
  directives, cell markers, the issue links that `TD003` reads, and the
  commented-out code that `ERA001` reports.
- Layout in comments: rulers, boxes, aligned columns and fenced blocks.
- The lines between `# fmt: off` and `# fmt: on`, or between `# yapf: disable`
  and `# yapf: enable`, and a docstring that a pragma follows, like
  `# fmt: skip`.
- In a docstring: the summary line, which `D205` holds to one line, code blocks,
  doctests and their output, the paragraph after Click's `\\b`, tables, section
  titles, reST explicit markup and option lists, Markdown headings and link
  definitions, NumPy `name : type` lines, and a closing line that code or a
  comment follows.
- A line whose lead and first word already pass the limit.

A break never opens a line with a doctest prompt, a list marker, a field or role
name, a reST or Markdown marker, a pragma or a task tag: it moves one word
earlier.
```

```{note}
Left for a human to reword: Google `Args:` items, NumPy descriptions, the second
paragraph of a list item, prose inside MyST ``{note}`` fences and `.. note::`
bodies, `#:` doc comments, CJK text without spaces, and tab-indented files.
```
"""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import string
import tempfile
import tokenize
import unicodedata
import warnings
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from click_extra.markdown.blocks import fence_spans

from .file_inventory import FileInventory
from .tooling.runner import run_tool

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator, Mapping

PYTHON_SUFFIXES = frozenset({".py", ".pyi", ".pyw"})
"""The sources this rewrites: notebooks and Cython files do not parse as Python."""

RUFF_RULES = ("W505", "ERA001")
"""The rules that the check adds to the rule selection of the project.

`W505` reports the doc lines past `max-doc-length`, and stays silent when the
project sets none. `ERA001` reports the commented-out code, which a wrap must not
split.
"""

OVERLONG_RULES = frozenset({"E501", "W505"})
"""The rules whose lines the wrap shortens."""

TAB_SIZE = 4
"""The tab width that Ruff measures with, when `indent-width` keeps its default."""

FIELD_HANG = 4
"""How far the next lines of a reST field indent past the field name."""

RECHECK_PASSES = 5
"""The most times Ruff checks the written files again.

Each check gives at least one block its old lines back, so a file with many
blocks could need many checks. Past this number, a file that still gains a
finding goes back to how it was, whole, with a warning.
"""

ARGUMENT_BUDGET = 8000
"""The most characters of paths that one Ruff command line takes.

Windows caps a command line at 32767 characters, so a long list of paths goes to
Ruff in batches.
"""

_COMMENT_HEAD = re.compile(r" *#+ ")
"""The indentation and hash marks of a comment line, with the space after them.

A comment with no space after its hash marks, like `#:` or `#!`, never matches.
"""

_ROMAN = r"(?=[ivxlcdm])m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
"""A roman number in lower case, like `iv`, and not a word like `did`."""

_ENUMERATOR = rf"(?:\d+|[A-Za-z]|{_ROMAN}|{_ROMAN.upper()}|#)"
"""An arabic, alphabetic or roman number of a list item, or the `#` of reST."""

_LIST_ITEM = re.compile(
    rf"(?:(?P<bullet>[-*+])|(?P<number>{_ENUMERATOR})[.)]"
    rf"|\((?P<inner>{_ENUMERATOR})\)) +"
)
"""The marker of a list item, which opens a paragraph of its own."""

_FIRST_ENUMERATORS = frozenset({"1", "a", "A", "i", "I", "#"})
"""The enumerators that open a list, so an item with one can interrupt a paragraph."""

_LIST_START = re.compile(r"(?:[-*+]|(?:1|[aAiI#])[.)]|\((?:1|[aAiI#])\))(?: |$)")
"""A marker that opens a list in the middle of a paragraph: a bullet, or a first
enumerator. CommonMark lets no other enumerator interrupt a paragraph, and reST
wants a blank line before any list."""

_FIELD = re.compile(r":[^:\s][^:]*: +")
"""The name of a reST field, like `:param name:`, which opens a paragraph too."""

_OPENING_MARKUP = re.compile(r">>>|\.\.|\||#|:[^:\s][^:]*:")
"""Text that no break may move to the start of a line: a doctest prompt, `...`,
reST markup, a line block or table row, a hash mark, and a field or role name."""

_DOCSTRING_MARKUP = re.compile(
    r"\"\"\"|'''|>>>|\.\.|\|"
    r"|--?[A-Za-z0-9]"
    r"|#{1,6}(?: |$)"
    r"|\[[^\]]+\]:(?: |$)"
    r"|\*{0,2}[\w.]+(?:, *\*{0,2}[\w.]+)* : "
)
"""The start of a docstring line that is no prose of a paragraph.

In order: closing quotes, a doctest prompt, reST explicit markup and the `...` of
a doctest, a line block or a table row, a reST option, a Markdown heading, a
Markdown link reference definition, and a NumPy `name : type` line.
"""

_TABLE_BORDER = re.compile(
    r"=+(?: +=+)+|-+(?: +-+)+|\+(?:[-=]+\+)+|\|? *:?-+:? *(?:\| *:?-+:? *)+\|?"
)
"""A border of a reST simple or grid table, or the delimiter row of a pipe table."""

_ADORNMENTS = frozenset(string.punctuation) - set("\"'`.>\\")
"""The characters that underline a reST or Markdown section title.

A run of quotes closes a docstring, a run of backticks opens a fence, a backslash
joins lines, and `...` and `>>>` belong to a doctest.
"""

_RULER = re.compile(r"(\S)\1{4,}")
"""A run of five or more of one character, which draws a ruler when it is no word."""

_ALIGNED = re.compile(r"(?<=\S)(?<![.!?])(?<![.!?][\"')\]]) {2,}(?=\S)")
"""A run of spaces between two words that aligns a column.

Two spaces after the end of a sentence are sentence spacing, not a column.
"""

_NAMED_ESCAPE = re.compile(r"\\N\{[^}]*\}")
"""A named character escape, one word whatever spaces its name holds."""

_RUFF_PRAGMAS = frozenset({
    "flake8",
    "isort",
    "pylint",
    "pyrefly",
    "pyright",
    "ruff",
    "ty",
    "type",
})
"""The words that open a pragma before a colon, as `is_pragma_comment` of Ruff
reads them."""

_TOOL_PRAGMAS = frozenset({"fmt", "mypy", "pragma", "yapf"})
"""More words that open a pragma before a colon: formatters, mypy and coverage."""

_REGION_MARKERS = {
    ("fmt", "off"): True,
    ("fmt", "on"): False,
    ("yapf", "disable"): True,
    ("yapf", "enable"): False,
}
"""The comments that open or close a region the formatters leave as it is."""

_CODING = re.compile(r"[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")
"""A PEP 263 encoding declaration, which counts on the first two lines only."""

_MODELINE = re.compile(r"(?:^|\s)(?:vim?|ex):")
"""A Vim modeline, which a blank must precede."""

_SPELL_DIRECTIVE = re.compile(
    r"(?<![\w-])(?:cspell|spell-checker|codespell):", re.IGNORECASE
)
"""A directive of cspell or codespell."""

_ISSUE_LINK = re.compile(r"#\s*(?:https?://|\d+$|[A-Z]+-?\d+$)")
"""A comment line that `TD003` reads as the issue link of a task comment above."""

_TASK_TAGS = frozenset({"fixme", "hack", "todo", "xxx"})
"""The words that open a task comment, as Ruff's `TodoDirective` reads them."""

_RUFF_LIMIT = re.compile(r"\((\d+) > (\d+)\)")
"""The width and the limit in the message of `E501` and `W505`."""

_ZERO_WIDTH = frozenset({"Cc", "Cf", "Me", "Mn"})
"""The categories of the characters that take no column: controls, format
characters, and the combining marks, variation selectors included."""


class RuffError(Exception):
    """Ruff cannot check the files, so no line can be picked."""


@cache
def _char_width(char: str) -> int:
    """Measure one character that is no tab, as Ruff does."""
    if unicodedata.category(char) in _ZERO_WIDTH or "\u1160" <= char <= "\u11ff":
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def line_width(text: str, tab_size: int = TAB_SIZE) -> int:
    """Measure *text* as `E501` does.

    A port of `LineWidthBuilder` from
    [`crates/ruff_linter/src/line_width.rs`](https://github.com/astral-sh/ruff/blob/8f0189e0e975334474efb938eafc48f22855aadb/crates/ruff_linter/src/line_width.rs):
    a combining mark, a format character and a variation selector take no
    column, a wide or fullwidth East Asian character takes two, and a tab runs
    to the next tab stop. As in Ruff, the tab stop counts characters, not
    columns.

    :param text: The line, without its line ending.
    :param tab_size: The tab width.
    :return: The width of the line.
    """
    if text.isascii() and text.isprintable():
        return len(text)
    width = column = 0
    for char in text:
        if char == "\t":
            offset = tab_size - column % tab_size
            width += offset
            column += offset
        else:
            width += _char_width(char)
            column += 1
    return width


def _too_long(line: str, limit: int, tab_size: int) -> bool:
    """Tell whether `E501` reports *line* at *limit*.

    `E501` skips a line that holds a single word, an SPDX header, and a line
    that ends with a URL starting inside the limit.
    """
    width = line_width(line, tab_size)
    if width <= limit:
        return False
    chunks = line.split()
    if len(chunks) < 2:
        return False
    if chunks[0] == "#" and chunks[1] in (
        "SPDX-License-Identifier:",
        "SPDX-FileCopyrightText:",
    ):
        return False
    return not (
        "://" in chunks[-1] and width - line_width(chunks[-1], tab_size) <= limit
    )


def is_overlong(line: str, limit: int, tab_size: int = TAB_SIZE) -> bool:
    """Tell whether `E501` reports *line* at *limit*, and a wrap can shorten it.

    `E501` skips a line that holds a single word, an SPDX header, and a line that
    ends with a URL starting inside the limit. It reports a comment of a single
    word, but no wrap can shorten that line, so this skips it too.

    :param line: The line, without its line ending.
    :param limit: The longest width a line may take.
    :param tab_size: The tab width.
    :return: Whether the line is too long and could be shorter.
    """
    chunks = line.split()
    if len(chunks) == 2 and chunks[0] == "#":
        return False
    return _too_long(line, limit, tab_size)


def _is_pragma(comment: str) -> bool:
    """Tell whether *comment*, from one of its hash marks, opens a pragma.

    It mirrors `is_pragma_comment` of
    [`crates/ruff_python_trivia/src/pragmas.rs`](https://github.com/astral-sh/ruff/blob/8f0189e0e975334474efb938eafc48f22855aadb/crates/ruff_python_trivia/src/pragmas.rs):
    `noqa` in any case, `nosec`, or one of {data}`_RUFF_PRAGMAS` before a colon,
    in that case. So `# Pragmatic ...` and `# Type: ...` are prose. It adds the
    words of {data}`_TOOL_PRAGMAS` and the `-*-` of an Emacs mode line.
    """
    content = comment[1:].lstrip()
    if content[:4].lower() == "noqa" or content.startswith(("nosec", "-*-")):
        return True
    word, colon, _ = content.partition(":")
    return bool(colon) and (word in _RUFF_PRAGMAS or word in _TOOL_PRAGMAS)


def _holds_pragma(comment: str) -> bool:
    """Tell whether a pragma opens at one of the hash marks of *comment*.

    As `find_trailing_pragma_offset` of Ruff, it finds a pragma after the text
    of a comment too, like a `# noqa` behind it.
    """
    return any(
        _is_pragma(comment[index:]) for index, char in enumerate(comment) if char == "#"
    )


def _opens_task(text: str) -> bool:
    """Tell whether *text* opens with a task tag, like `TODO`, in any case."""
    match = re.match(r"[^\W_]*", text)
    return match is not None and match.group().lower() in _TASK_TAGS


def _machine_read(comment: str, row: int) -> bool:
    """Tell whether a tool reads the comment line at *row*, so its words stay.

    :param comment: The comment, from its first hash mark.
    :param row: The row of the comment, from 0.
    """
    content = comment.lstrip("#").lstrip()
    return (
        _holds_pragma(comment)
        or content.startswith("%%")
        or (row < 2 and _CODING.match(comment) is not None)
        or _MODELINE.search(comment) is not None
        or _SPELL_DIRECTIVE.search(comment) is not None
        or _ISSUE_LINK.match(comment) is not None
    )


def _draws_layout(text: str) -> bool:
    """Tell whether the text of a comment line draws a ruler, a box or a column.

    :param text: The comment, after its hash marks and the space after them.
    """
    if any(not match.group(1).isalnum() for match in _RULER.finditer(text)):
        return True
    if text.rstrip().endswith("#") or "|" in text or "+--" in text or "--+" in text:
        return True
    if any("\u2500" <= char <= "\u259f" for char in text):
        return True
    item = _LIST_ITEM.match(text.lstrip(" "))
    words = text.lstrip(" ")[item.end() if item else 0 :]
    return _ALIGNED.search(words) is not None


def _opens_badly(opening: str, kind: str) -> bool:
    """Tell whether a line whose words open with *opening* would change meaning.

    In both kinds of block, it would read as {data}`_OPENING_MARKUP` or open a
    list. In a docstring, it would read as {data}`_DOCSTRING_MARKUP` too. In a
    comment, it would read as a cell marker, a pragma, a modeline, a
    spell-checker directive or a task comment.

    :param opening: The words of the line, after its hang.
    :param kind: `comment` or `docstring`.
    """
    if _OPENING_MARKUP.match(opening) or _LIST_START.match(opening):
        return True
    if kind == "docstring":
        return _DOCSTRING_MARKUP.match(opening) is not None
    comment = f"# {opening}"
    return (
        opening.startswith("%%")
        or _is_pragma(comment)
        or _MODELINE.search(comment) is not None
        or _SPELL_DIRECTIVE.search(comment) is not None
        or _opens_task(opening)
    )


def _list_item(
    text: str, start: int, opens_paragraph: bool, in_list: bool
) -> re.Match[str] | None:
    """Match the marker of a list item at *start* of *text*, if one opens there.

    A bullet opens an item anywhere. Another enumerator than one of
    {data}`_FIRST_ENUMERATORS` opens an item only at the start of a paragraph or
    inside a list: elsewhere, like `500)` in `(vs default 500) so`, it goes on
    with the sentence above.

    :param opens_paragraph: Whether a blank line or nothing precedes the line.
    :param in_list: Whether the line above is a list item or its next line.
    """
    match = _LIST_ITEM.match(text, start)
    if match is None or match.group("bullet") or opens_paragraph or in_list:
        return match
    number = match.group("number") or match.group("inner")
    return match if number in _FIRST_ENUMERATORS else None


def _words(text: str) -> list[str]:
    """Split *text* at each space, but keep a named escape as one word.

    A run of spaces gives empty words, so a join gives the same text back.
    """
    protected = _NAMED_ESCAPE.sub(lambda match: match.group().replace(" ", "\0"), text)
    return [word.replace("\0", " ") for word in protected.split(" ")]


@dataclass
class _Line:
    """One line of a source file, and how the wrap may rewrite it."""

    text: str
    """The line, without its line ending."""

    origin: int | None = None
    """The row of the line in the source, or `None` for a line the wrap adds."""

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
        """Tell whether this line goes on with the docstring paragraph of *previous*."""
        return (
            self.group is not None
            and self.group[0] == "docstring"
            and self.group == previous.group
            and self.lead == previous.hang
            and self.hang == previous.hang
        )


def _indent(text: str) -> int:
    """Count the spaces that open *text*."""
    return len(text) - len(text.lstrip(" "))


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


def _quiet_rows(tokens: list[tokenize.TokenInfo], lines: list[_Line]) -> set[int]:
    """Find the rows between `# fmt: off` and `# fmt: on`, or the yapf pair.

    Only a comment on a line of its own opens or closes a region. A region that
    no comment closes runs to the end of the file.
    """
    rows: set[int] = set()
    start: int | None = None
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        row, column = token.start[0] - 1, token.start[1]
        if lines[row].text[:column].strip():
            continue
        word, _, command = token.string[1:].strip().partition(":")
        opens = _REGION_MARKERS.get((word, command.strip()))
        if opens and start is None:
            start = row
        elif opens is False and start is not None:
            rows.update(range(start, row + 1))
            start = None
    if start is not None:
        rows.update(range(start, len(lines)))
    return rows


def _mark_comments(
    tokens: list[tokenize.TokenInfo],
    lines: list[_Line],
    spans: dict[tuple[str, int], tuple[int, int]],
    code_rows: Collection[int],
) -> None:
    """Mark the full-line comments, block by block.

    A block is a run of comment lines at one column. A trailing comment shares
    its line with code, and is no part of a block. Inside a block, a line that a
    tool reads, that draws a layout, that sits in a fenced block or that holds
    commented-out code keeps its words. A line indented past its hash marks is
    rewritten only as the next line of a list item: any other such line is a
    sample, like a command or a table.

    :param spans: Gets the first and last row of each block.
    :param code_rows: The rows of commented-out code, from 0.
    """
    columns: dict[int, int] = {}
    comments: dict[int, str] = {}
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        row, column = token.start[0] - 1, token.start[1]
        before = lines[row].text[:column]
        if before.strip() or "\t" in before or token.string.startswith("#!"):
            continue
        columns[row] = column
        comments[row] = token.string

    blocks: list[list[int]] = []
    for row in sorted(columns):
        if blocks and blocks[-1][-1] == row - 1 and columns[row - 1] == columns[row]:
            blocks[-1].append(row)
        else:
            blocks.append([row])

    for rows in blocks:
        group = ("comment", rows[0])
        spans[group] = (rows[0], rows[-1])
        heads = [_COMMENT_HEAD.match(lines[row].text) for row in rows]
        texts = [
            lines[row].text[head.end() :] if head else ""
            for row, head in zip(rows, heads)
        ]
        fences = fence_spans(texts, chars="`~")
        fenced = {
            rows[index]
            for span in fences.values()
            for index in range(span.start, span.end)
        }
        hang: int | None = None
        opens_paragraph = True
        for row, head, text in zip(rows, heads, texts):
            line = lines[row]
            after_blank, opens_paragraph = opens_paragraph, not text.strip()
            if (
                head is None
                or not text.strip()
                or row in fenced
                or row in code_rows
                or text.startswith("#")
                or _machine_read(comments[row], row)
                or _draws_layout(text)
            ):
                hang = None
                continue
            start = head.end()
            inner = start + _indent(text)
            item = _list_item(line.text, inner, after_blank, hang is not None)
            if item:
                hang = item.end()
                _mark(line, group, start, item.end(), hang)
            elif inner == start:
                hang = None
                _mark(line, group, start, start, start)
            elif hang is not None and inner == hang:
                _mark(line, group, start, hang, hang)


def _string_statements(tree: ast.AST) -> Iterator[tuple[ast.Expr, ast.Constant]]:
    """Yield each string statement of *tree* with its string: the docstrings."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement in body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                yield statement, statement.value


def _columns(text: str, offset: int) -> int:
    """Turn the byte *offset* that `ast` gives into a column of *text*."""
    return len(text.encode("UTF-8")[:offset].decode("UTF-8", errors="ignore"))


def _paragraphs(lines: list[_Line], rows: range) -> Iterator[list[int]]:
    """Yield the rows of each paragraph: each run of lines that are not blank."""
    paragraph: list[int] = []
    for row in rows:
        if lines[row].text.strip():
            paragraph.append(row)
        elif paragraph:
            yield paragraph
            paragraph = []
    if paragraph:
        yield paragraph


def _is_underline(text: str) -> bool:
    """Tell whether *text* underlines a section title, like `=====`."""
    return len(text) >= 3 and text[0] in _ADORNMENTS and not text.strip(text[0])


def _whole_paragraph_rows(lines: list[_Line], rows: range) -> set[int]:
    """Find the docstring rows that their paragraph keeps whole.

    That is each row of a paragraph that holds a table border, the rows from
    Click's `\\b` or from a doctest prompt to the end of the paragraph, and a
    section title with its underline.
    """
    whole: set[int] = set()
    for paragraph in _paragraphs(lines, rows):
        texts = [lines[row].text.strip() for row in paragraph]
        if any(_TABLE_BORDER.fullmatch(text) for text in texts):
            whole.update(paragraph)
            continue
        for index, text in enumerate(texts):
            if text == "\\b" or text.startswith(">>>"):
                whole.update(paragraph[index:])
                break
            if _is_underline(text):
                whole.update(paragraph[max(index - 1, 0) : index + 1])
    return whole


def _continuation(lines: list[_Line], row: int, end: int, indent: int) -> int | None:
    """Return the indentation of the line after *row* when it goes on with it.

    :param end: The row after the last one of the docstring.
    :param indent: The indentation of the line at *row*.
    """
    following = row + 1
    if following >= end or not lines[following].text.strip():
        return None
    text = lines[following].text
    inner = _indent(text)
    if inner <= indent or _LIST_ITEM.match(text, inner) or _FIELD.match(text, inner):
        return None
    return inner


def _mark_docstring(
    lines: list[_Line], group: tuple[str, int], first: int, last: int, suffix: str
) -> None:
    """Mark the rows of one docstring that the wrap may rewrite.

    The summary is left out, and so is every block that a wrap would break: a
    fenced block, a block indented after a blank line, a doctest, a table, and
    the markup that {data}`_DOCSTRING_MARKUP` matches. A line indented past the
    text is rewritten only as the next line of a list item or of a reST field.
    Such a field indents {data}`FIELD_HANG` columns past its name, unless its
    next line already sets another hang.

    A docstring that does not open on a triple quote is left out whole, like
    literals that a pair of parentheses joins: a line break inside a
    single-quoted literal is a syntax error. So is a docstring that a pragma
    follows, which applies to all its lines.

    :param suffix: The text after the closing quotes.
    """
    opening = lines[first].text.strip().lstrip("rRbBuUfF")
    if not opening.startswith(('"""', "'''")):
        return
    trailing = suffix.strip()
    if _holds_pragma(trailing):
        return
    summary = first if opening.strip("\"'") else first + 1
    # Code or a comment after the closing quotes stays on their line.
    end = last if trailing else last + 1
    rows = range(summary + 1, end)
    indents = [_indent(lines[row].text) for row in rows if lines[row].text.strip()]
    if not indents:
        return
    base = min(indents)
    # Only a bare fence of its own character, at least as long, closes a block:
    # a shorter fence inside it is one more line of that block.
    spans = fence_spans([lines[row].text for row in rows], chars="`~")
    fenced = {rows[i] for span in spans.values() for i in range(span.start, span.end)}
    whole = fenced | _whole_paragraph_rows(lines, rows)
    blocked = False
    blank_before = True
    hang: int | None = None
    for row in rows:
        line = lines[row]
        text = line.text.strip()
        if not text:
            blank_before, hang = True, None
            continue
        indent = _indent(line.text)
        after_blank, blank_before = blank_before, False
        if indent > base and after_blank:
            blocked = True
        elif indent == base:
            blocked = False
        if (
            blocked
            or row in whole
            or "\t" in line.text
            or text.endswith("\\")
            or _DOCSTRING_MARKUP.match(text)
        ):
            hang = None
            continue
        item = _list_item(line.text, indent, after_blank, hang is not None)
        field = _FIELD.match(line.text, indent)
        if item:
            hang = _continuation(lines, row, end, indent) or item.end()
            _mark(line, group, indent, item.end(), hang)
        elif field:
            hang = _continuation(lines, row, end, indent) or indent + FIELD_HANG
            _mark(line, group, indent, field.end(), hang)
        elif indent == base:
            hang = None
            _mark(line, group, indent, indent, indent)
        elif indent == hang:
            _mark(line, group, indent, indent, indent)


def _break(line: _Line, limit: int, tab_size: int, rest: str) -> int | None:
    """Find the word where *line* breaks, or `None` to keep it whole.

    The break leaves the most words that fit. It moves one word earlier while the
    line it opens would change meaning, while that line could not take the first
    moved word, and while the line it ends ends with a backslash, which would
    join the next line in a string.

    :param rest: The words of the next line, when the moved words open it.
    :return: The number of words the line keeps.
    """
    words = _words(line.text[len(line.lead) :])
    first = next((word for word in words if word), "")
    if line_width(line.lead + first, tab_size) > limit:
        return None
    cut = len(words)
    while cut > 1 and line_width(line.lead + " ".join(words[:cut]), tab_size) > limit:
        cut -= 1
    kind = line.group[0] if line.group is not None else "comment"
    while cut > 0:
        kept = " ".join(words[:cut]).rstrip(" ")
        carried = " ".join(words[cut:]).strip(" ")
        opening = f"{carried} {rest}" if rest else carried
        if (
            kept.strip()
            and carried
            and not kept.endswith("\\")
            and not _opens_badly(opening, kind)
            and line_width(line.hang + _words(carried)[0], tab_size) <= limit
        ):
            return cut
        cut -= 1
    return None


def _push_down(lines: list[_Line], row: int, limit: int, tab_size: int) -> int:
    """Shorten the line at *row*, and each line its words then overflow.

    In a comment, the words open a new line below. In a docstring, they open the
    next line of the paragraph, or a new line below the last one.

    :return: How many lines got shorter.
    """
    shortened = 0
    while row < len(lines) and is_overlong(lines[row].text, limit, tab_size):
        line = lines[row]
        following = lines[row + 1] if row + 1 < len(lines) else None
        merge = following is not None and following.continues(line)
        rest = following.text[len(line.hang) :] if following and merge else ""
        cut = _break(line, limit, tab_size, rest)
        if cut is None:
            break
        words = _words(line.text[len(line.lead) :])
        # The spaces at the break go with it, so neither line ends or opens on
        # one: a run of them splits into empty words.
        carried = " ".join(words[cut:]).strip(" ")
        line.text = line.lead + " ".join(words[:cut]).rstrip(" ")
        shortened += 1
        if following is not None and merge:
            following.text = f"{line.hang}{carried} {rest}"
        else:
            lines.insert(
                row + 1,
                _Line(
                    f"{line.hang}{carried}",
                    group=line.group,
                    lead=line.hang,
                    hang=line.hang,
                ),
            )
        row += 1
    return shortened


@dataclass(frozen=True)
class _Block:
    """One comment block or docstring, with the lines its wrap gives it."""

    group: tuple[str, int]
    """The kind of the block, and the row it starts at, from 0."""

    first: int
    """The first row of the block in the source, from 0."""

    last: int
    """The last row of the block in the source, from 0."""

    lines: tuple[str, ...]
    """The lines of the block after the wrap."""

    shortened: int
    """How many of its lines got shorter."""

    kept: frozenset[int]
    """The rows of the source that the wrap left as they were, from 0."""

    touched: tuple[bool, ...]
    """For each of {attr}`lines`, whether the wrap changed or added it."""


@dataclass
class _Plan:
    """The wrap of one source, block by block."""

    lines: list[str]
    """The lines of the source."""

    blocks: list[_Block] = field(default_factory=list)
    """The blocks whose wrap changed them and keeps their code."""

    dropped: list[tuple[int, str]] = field(default_factory=list)
    """Each block left as it was, as its first row from 1 and the reason."""

    def text(self, blocks: Iterable[_Block] | None = None) -> str:
        """Join the source with the wrap of *blocks*, by default all of them."""
        chosen = sorted(self.blocks if blocks is None else blocks, key=_first_row)
        out: list[str] = []
        row = 0
        for block in chosen:
            out.extend(self.lines[row : block.first])
            out.extend(block.lines)
            row = block.last + 1
        out.extend(self.lines[row:])
        return "\n".join(out)


def _first_row(block: _Block) -> int:
    """Sort blocks by the row they start at."""
    return block.first


def _string_value(rows: Iterable[str], start: int, suffix: int) -> str | None:
    """Evaluate the string literal that *rows* hold, with its whitespace collapsed.

    :param start: The column where the literal starts on the first row.
    :param suffix: How many characters follow the literal on the last row.
    :return: The value of the string, or `None` when the text is no string.
    """
    text = "\n".join(rows)
    segment = text[start : len(text) - suffix]
    try:
        with warnings.catch_warnings():
            # A backslash that the wrap moved can raise a SyntaxWarning first.
            warnings.simplefilter("ignore")
            node = ast.parse(f"({segment})", mode="eval").body
    except (SyntaxError, ValueError):
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return " ".join(node.value.split())
    return None


def _code(tree: ast.AST) -> str:
    """Dump *tree* with the whitespace of each docstring collapsed.

    A comment is no node of the tree, and a wrap moves words only across the
    line breaks of a docstring. So the dumps of a source before and after its
    wrap are equal, unless the wrap changed the code.
    """
    for _, constant in _string_statements(tree):
        if isinstance(constant.value, str):
            constant.value = " ".join(constant.value.split())
    return ast.dump(tree)


def _keeps_code(source: str, wrapped: str) -> bool:
    """Tell whether *wrapped* holds the code of *source*, with only its prose moved."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _code(ast.parse(wrapped)) == _code(ast.parse(source))
    except (SyntaxError, ValueError):
        return False


def _plan(
    source: str,
    limit: int,
    rows: Mapping[int, int] | None = None,
    code_rows: Collection[int] = (),
    tab_size: int = TAB_SIZE,
) -> _Plan:
    """Wrap the overlong comment and docstring lines of *source*, block by block.

    See {func}`wrap_source` for the parameters.

    :raises SyntaxError: when *source* does not parse as Python.
    """
    texts = source.split("\n")
    lines = [_Line(text, origin) for origin, text in enumerate(texts)]
    with warnings.catch_warnings():
        # An invalid escape in a docstring raises a SyntaxWarning, not an error.
        warnings.simplefilter("ignore")
        tree = ast.parse(source)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    spans: dict[tuple[str, int], tuple[int, int]] = {}
    _mark_comments(tokens, lines, spans, {row - 1 for row in code_rows})
    strings: dict[tuple[str, int], tuple[int, int]] = {}
    for statement, _ in _string_statements(tree):
        if statement.end_lineno is None or statement.end_col_offset is None:
            continue
        first, last = statement.lineno - 1, statement.end_lineno - 1
        docstring = ("docstring", first)
        spans[docstring] = (first, last)
        start = _columns(texts[first], statement.col_offset)
        end = _columns(texts[last], statement.end_col_offset)
        strings[docstring] = (start, len(texts[last]) - end)
        _mark_docstring(lines, docstring, first, last, texts[last][end:])
    for row in _quiet_rows(tokens, lines):
        lines[row].group = None

    if rows is None:
        reported = {
            row: limit
            for row, line in enumerate(lines)
            if line.group is not None and is_overlong(line.text, limit, tab_size)
        }
    else:
        reported = {
            row - 1: min(limit, row_limit)
            for row, row_limit in rows.items()
            if 0 < row <= len(lines)
        }
    limits: dict[tuple[str, int], int] = {}
    for row, row_limit in reported.items():
        marked = lines[row].group
        if marked is not None:
            limits[marked] = min(row_limit, limits.get(marked, row_limit))

    shortened: Counter[tuple[str, int]] = Counter()
    # Bottom up: a push only ever inserts a line below the row it starts from.
    for row in reversed(range(len(lines))):
        marked = lines[row].group
        if marked is not None and marked in limits and row in reported:
            shortened[marked] += _push_down(lines, row, limits[marked], tab_size)

    # A line that the wrap adds belongs to the block of the line above it.
    owners = {row: key for key, (a, b) in spans.items() for row in range(a, b + 1)}
    news: dict[tuple[str, int], list[_Line]] = {}
    owner = 0
    for line in lines:
        if line.origin is not None:
            owner = line.origin
        key = owners.get(owner)
        if key is not None and shortened[key]:
            news.setdefault(key, []).append(line)

    plan = _Plan(texts)
    for group, block_lines in news.items():
        first, last = spans[group]
        touched = tuple(
            line.origin is None or line.text != texts[line.origin]
            for line in block_lines
        )
        block = _Block(
            group,
            first,
            last,
            tuple(line.text for line in block_lines),
            shortened[group],
            frozenset(
                line.origin
                for line, changed in zip(block_lines, touched)
                if not changed and line.origin is not None
            ),
            touched,
        )
        reason = None
        if any(
            changed and _too_long(text, limits[group], tab_size)
            for text, changed in zip(block.lines, block.touched)
        ):
            reason = "a wrapped line would still be too long"
        elif group in strings:
            start, suffix = strings[group]
            before = _string_value(texts[first : last + 1], start, suffix)
            if before is None or before != _string_value(block.lines, start, suffix):
                reason = "the wrap would change the string"
        if reason:
            plan.dropped.append((first + 1, reason))
        else:
            plan.blocks.append(block)
    plan.blocks.sort(key=_first_row)
    plan.dropped.sort()

    # Each block keeps its own code, so this guard of the whole file is a last
    # resort that no known source reaches.
    if plan.blocks and not _keeps_code(source, plan.text()):
        plan.dropped.extend(
            (block.first + 1, "the wrap would change the code") for block in plan.blocks
        )
        plan.blocks = []
    return plan


def wrap_source(
    source: str,
    limit: int,
    rows: Mapping[int, int] | None = None,
    *,
    code_rows: Collection[int] = (),
    tab_size: int = TAB_SIZE,
) -> tuple[str, int]:
    """Wrap the overlong comment and docstring lines of one Python source.

    The wrap calls no tool: {func}`fix_long_lines` passes it what Ruff reports.

    :param source: The text of the file.
    :param limit: The longest width a line may take.
    :param rows: The rows that `E501` or `W505` reports, from 1, each with the
        limit of its message, which can only lower *limit*. Left as `None`, the
        wrap measures each line with {func}`line_width` against *limit*.
    :param code_rows: The rows of commented-out code, from 1, that `ERA001`
        reports. No word leaves them.
    :param tab_size: The tab width.
    :return: The new text, and how many lines got shorter. A block whose wrap
        would change its code keeps its lines.
    :raises SyntaxError: when *source* does not parse as Python.
    """
    plan = _plan(source, limit, rows, code_rows, tab_size)
    return plan.text(), sum(block.shortened for block in plan.blocks)


@dataclass(frozen=True)
class _Finding:
    """One finding of Ruff."""

    code: str
    """The rule code, or the name of the finding when it has no code."""

    row: int
    """The row of the finding, from 1."""

    message: str
    """The message of the finding."""


def _batches(paths: list[Path]) -> Iterator[list[Path]]:
    """Split *paths* into lists that fit {data}`ARGUMENT_BUDGET`."""
    batch: list[Path] = []
    size = 0
    for path in paths:
        length = len(str(path)) + 1
        if batch and size + length > ARGUMENT_BUDGET:
            yield batch
            batch, size = [], 0
        batch.append(path)
        size += length
    if batch:
        yield batch


def _ruff_findings(
    paths: list[Path], line_length: int | None
) -> dict[Path, list[_Finding]]:
    """Check *paths* with the pinned Ruff, through {func}`run_tool`.

    The run resolves the configuration as `repomatic run ruff` does, so a
    repository with none gets the bundled default. It adds {data}`RUFF_RULES`
    to the rule selection, and fixes nothing. Ruff writes its JSON report into
    a temporary directory outside the project, removed at the end, and reads no
    cache: Ruff keys its cache on the modification time of a file, so a file
    written twice in one clock tick would read as unchanged.

    :param paths: The files to check.
    :param line_length: When set, adds `E501` and sets its limit.
    :return: The findings of each resolved path.
    :raises RuffError: when Ruff does not run to its end.
    """
    rules = [*RUFF_RULES, "E501"] if line_length else list(RUFF_RULES)
    args = [
        "check",
        "--no-cache",
        "--no-fix",
        "--exit-zero",
        "--output-format",
        "json",
        "--extend-select",
        ",".join(rules),
    ]
    if line_length:
        args += ["--config", f"lint.pycodestyle.max-line-length = {line_length}"]
    findings: dict[Path, list[_Finding]] = {}
    with tempfile.TemporaryDirectory(prefix="repomatic-fix-long-lines-") as folder:
        report = Path(folder) / "ruff.json"
        for batch in _batches(paths):
            report.unlink(missing_ok=True)
            command = [*args, "--output-file", str(report), "--", *map(str, batch)]
            exit_code = run_tool("ruff", command)
            if exit_code:
                msg = f"Ruff exited with code {exit_code}."
                raise RuffError(msg)
            content = report.read_text(encoding="UTF-8") if report.exists() else ""
            for item in json.loads(content or "[]"):
                path = Path(item["filename"]).resolve()
                findings.setdefault(path, []).append(
                    _Finding(
                        item.get("code") or item.get("name") or "",
                        item["location"]["row"],
                        item["message"],
                    )
                )
    return findings


def _overlong(findings: Iterable[_Finding]) -> dict[int, tuple[int, int]]:
    """Read the width and the limit of each row that `E501` or `W505` reports."""
    rows: dict[int, tuple[int, int]] = {}
    for finding in findings:
        match = _RUFF_LIMIT.search(finding.message)
        if finding.code in OVERLONG_RULES and match:
            width, limit = int(match.group(1)), int(match.group(2))
            if finding.row not in rows or limit < rows[finding.row][1]:
                rows[finding.row] = (width, limit)
    return rows


def _tab_size(lines: list[str], overlong: Mapping[int, tuple[int, int]]) -> int:
    """Find the tab width that gives a reported line with a tab its Ruff width.

    Ruff measures a tab to the next stop of its `indent-width`, which the
    message of `E501` holds in the width it gives.
    """
    for row, (width, _) in overlong.items():
        text = lines[row - 1] if row <= len(lines) else ""
        if "\t" in text:
            for size in range(1, 17):
                if line_width(text, size) == width:
                    return size
    return TAB_SIZE


def _decode(path: Path) -> tuple[bytes, str, str] | None:
    """Read *path* in the encoding of its PEP 263 cookie, as `tokenize.open` does.

    :return: The bytes, the encoding and the text, or `None` with a warning
        when the file does not decode. A UTF-8 file with a byte order mark reads
        as `utf-8-sig`, which writes the mark back.
    """
    data = path.read_bytes()
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data, encoding, data.decode(encoding)
    except (SyntaxError, LookupError, UnicodeDecodeError) as exc:
        logging.warning(f"Leaving {path} as it is: it does not decode ({exc}).")
        return None


@dataclass
class _Candidate:
    """A file, with the wrap of its blocks that Ruff has to check."""

    path: Path
    """The file."""

    original: bytes
    """The content of the file before the wrap."""

    source: str
    """The text of the file before the wrap."""

    encoding: str
    """The encoding to write the file in."""

    plan: _Plan
    """The wrap of the file."""

    before: list[_Finding]
    """The findings of Ruff on the file before the wrap."""

    blocks: list[_Block] = field(default_factory=list)
    """The blocks whose wrap the file holds."""

    left: list[tuple[int, str, int | None]] = field(default_factory=list)
    """Each block left as it was: its first row in the source, from 1, the
    reason, and the row from 1 of the finding outside it that the reason names."""

    given_up: str | None = None
    """Why the whole file went back to how it was, or `None`."""

    touched: bool = False
    """Whether the run wrote the file."""

    def row(self, row: int) -> int:
        """Map a row of the source, from 1, to its row in the file as written."""
        return row + sum(
            len(block.lines) - (block.last - block.first + 1)
            for block in self.blocks
            if block.last < row - 1
        )

    def warn(self) -> None:
        """Warn about each block left as it was, at its row in the file as written."""
        if self.given_up:
            logging.warning(f"Leaving {self.path} as it is: {self.given_up}.")
        for row, reason, line in sorted(self.left, key=lambda left: left[0]):
            if line is not None:
                reason = f"{reason} on line {self.row(line)} after the wrap"
            logging.warning(
                f"{self.path}:{self.row(row)}: left as it is: {reason}. "
                "Shorten it by hand."
            )

    def write(self) -> bool:
        """Write the file with the wrap of its blocks, if the wrap keeps its code.

        :return: Whether the text kept the code of the source, and was written.
        """
        text = self.plan.text(self.blocks)
        if not _keeps_code(self.source, text):
            return False
        self.touched = True
        self.path.write_bytes(text.encode(self.encoding))
        return True

    def restore(self) -> None:
        """Write the file as it was."""
        self.path.write_bytes(self.original)

    def give_up(self, reason: str) -> None:
        """Give every block its old lines back, and write the file as it was."""
        self.given_up = reason
        self.blocks = []
        if self.touched:
            self.restore()

    def offenders(self, after: list[_Finding]) -> dict[_Block, tuple[str, int | None]]:
        """Find the blocks whose wrap gave Ruff a new finding.

        A finding inside a block counts as new when the block holds more of its
        rule than before, or, for `E501` and `W505`, more than the lines the
        wrap left as they were. A new finding outside every block goes to the
        block nearest to it.

        :param after: The findings of Ruff on the file with the wrap.
        :return: Each block to give its old lines back, with the reason, and the
            row in the source, from 1, of the finding outside it.
        """
        # The block of each new row, or the old row of a line outside the blocks.
        owners: list[tuple[_Block | None, int]] = []
        ranges: dict[_Block, range] = {}
        row = 0
        for block in self.blocks:
            owners.extend((None, old) for old in range(row, block.first))
            ranges[block] = range(len(owners), len(owners) + len(block.lines))
            owners.extend((block, block.first) for _ in block.lines)
            row = block.last + 1
        owners.extend((None, old) for old in range(row, len(self.plan.lines)))

        inside: dict[_Block, Counter[str]] = {block: Counter() for block in self.blocks}
        strays: list[tuple[int, _Finding]] = []
        for finding in after:
            index = min(max(finding.row - 1, 0), len(owners) - 1)
            holder = owners[index][0]
            if holder is None:
                strays.append((index, finding))
            else:
                inside[holder][finding.code] += 1

        offenders: dict[_Block, tuple[str, int | None]] = {}
        for block in self.blocks:
            before = Counter(
                finding.code
                for finding in self.before
                if block.first < finding.row <= block.last + 1
                and (
                    finding.code not in OVERLONG_RULES or finding.row - 1 in block.kept
                )
            )
            new = [
                code for code, count in inside[block].items() if count > before[code]
            ]
            if new:
                offenders[block] = (f"Ruff reports {new[0]} on it after the wrap", None)

        known = Counter(
            (finding.code, finding.row - 1)
            for finding in self.before
            if not any(b.first < finding.row <= b.last + 1 for b in self.blocks)
        )
        for index, finding in strays:
            key = (finding.code, owners[index][1])
            if known[key] > 0:
                known[key] -= 1
                continue
            nearest = min(self.blocks, key=lambda b: _distance(index, ranges[b]))
            offenders.setdefault(
                nearest, (f"Ruff reports {finding.code}", owners[index][1] + 1)
            )
        return offenders


def _distance(index: int, rows: range) -> int:
    """Count the rows between *index* and the nearest row of *rows*."""
    return max(rows.start - index, index - rows[-1], 0)


def _default_paths() -> list[Path]:
    """List the Python sources of the repository."""
    return [
        path for path in FileInventory().python_files if path.suffix in PYTHON_SUFFIXES
    ]


def _candidate(
    path: Path, findings: list[_Finding], line_length: int | None
) -> _Candidate | None:
    """Plan the wrap of one file from what Ruff reports on it.

    :return: The file and its wrap, or `None` when nothing in it can change.
    """
    overlong = _overlong(findings)
    if not overlong:
        return None
    decoded = _decode(path)
    if decoded is None:
        return None
    original, encoding, source = decoded
    if "\r" in source:
        ending = "CRLF" if "\r\n" in source else "CR"
        logging.warning(f"Leaving {path} as it is: it has {ending} line endings.")
        return None
    rows = {row: limit for row, (_, limit) in overlong.items()}
    code_rows = {finding.row for finding in findings if finding.code == "ERA001"}
    try:
        plan = _plan(
            source,
            line_length or max(rows.values()),
            rows,
            code_rows,
            _tab_size(source.split("\n"), overlong),
        )
    except (SyntaxError, ValueError, tokenize.TokenError) as exc:
        logging.warning(f"Leaving {path} as it is: it does not parse ({exc}).")
        return None
    candidate = _Candidate(
        path, original, source, encoding, plan, findings, list(plan.blocks)
    )
    candidate.left.extend((row, reason, None) for row, reason in plan.dropped)
    if not plan.blocks:
        candidate.warn()
        return None
    return candidate


def fix_long_lines(
    paths: Iterable[Path] | None = None, line_length: int | None = None
) -> dict[Path, int]:
    """Wrap the comment and docstring lines of *paths* that Ruff reports as too long.

    Run it from the root of the project, where `repomatic run ruff` finds the
    configuration. A file that does not decode, that does not parse as Python,
    or that has CRLF or CR line endings, is left as it is, with a warning. So is
    each block whose wrap would change the code, or that Ruff, run again on the
    file, finds a new problem in.

    Ruff checks the files as written, so a block that goes back costs its file a
    second write. Each write parses its text first, and writes no text that
    changes the code. Ruff checks the files again at most {data}`RECHECK_PASSES`
    times. If the run stops on an error, like a Ruff that cannot finish, every
    file it wrote gets its old bytes back.

    :param paths: The files to rewrite. Defaults to the Python sources of the
        repository.
    :param line_length: Adds `E501` to the rule selection, at this limit.
        Defaults to the rules and limits of the Ruff configuration.
    :return: Each file that changed, with how many of its lines got shorter.
    :raises RuffError: when Ruff does not run to its end.
    :raises ToolInstallError: when the pinned Ruff cannot be installed.
    """
    targets = list(dict.fromkeys(_default_paths() if paths is None else paths))
    if not targets:
        return {}
    before = _ruff_findings(targets, line_length)
    pending: dict[Path, _Candidate] = {}
    for path in targets:
        candidate = _candidate(path, before.get(path.resolve(), []), line_length)
        if candidate is not None:
            pending[path] = candidate
    candidates = list(pending.values())

    report: dict[Path, int] = {}
    try:
        # Each check settles a file, or gives at least one of its blocks back.
        for check in range(1, RECHECK_PASSES + 1):
            for path, candidate in list(pending.items()):
                if not candidate.write():
                    candidate.give_up("the wrap would change its code")
                    del pending[path]
            if not pending:
                break
            after = _ruff_findings(list(pending), line_length)
            still: dict[Path, _Candidate] = {}
            for path, candidate in pending.items():
                offenders = candidate.offenders(after.get(path.resolve(), []))
                if not offenders:
                    report[path] = sum(block.shortened for block in candidate.blocks)
                    continue
                candidate.left.extend(
                    (block.first + 1, reason, line)
                    for block, (reason, line) in offenders.items()
                )
                candidate.blocks = [b for b in candidate.blocks if b not in offenders]
                if check == RECHECK_PASSES:
                    candidate.give_up(
                        f"Ruff still finds a new problem in it after {check} checks"
                    )
                elif candidate.blocks:
                    still[path] = candidate
                else:
                    candidate.restore()
            pending = still
    except BaseException:
        for candidate in candidates:
            if candidate.touched:
                candidate.restore()
        raise
    for candidate in candidates:
        candidate.warn()
    return {path: report[path] for path in targets if path in report}
