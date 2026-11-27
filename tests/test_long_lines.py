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

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click_extra.testing import CliRunner

from repomatic import long_lines
from repomatic.cli.main import repomatic
from repomatic.long_lines import (
    RuffError,
    fix_long_lines,
    is_overlong,
    line_width,
    wrap_source,
)
from repomatic.tooling.registry import ToolInstallError
from repomatic.tooling.runner import run_tool
from tests.conftest import skip_unless_tool_runs

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

LIMIT = 30
"""A short limit, so each case stays readable."""

LONG_COMMENT = (
    "# The apples ripen in the late summer sun of the valley, then the pears"
    " follow them in the autumn rain."
)
"""A comment past the 88 columns that Ruff checks when nothing sets a limit."""

FRENCH = "Les pommes m\u00fbrissent au soleil d'\u00e9t\u00e9."
"""A sentence that latin-1 can encode and ASCII cannot."""

FRENCH_WRAPPED = "Les pommes m\u00fbrissent au\n# soleil d'\u00e9t\u00e9."
"""The comment of {data}`FRENCH`, wrapped at {data}`LIMIT`."""


def _function(body: str) -> str:
    """Put *body*, whose lines indent by four, in the docstring of a function."""
    return f'def ripen():\n    """Ripen the fruit.\n\n{body}    """\n'


@pytest.mark.parametrize(
    ("text", "width"),
    (
        pytest.param("Apples ripen.", 13, id="ascii"),
        pytest.param("Pe\u0301che", 5, id="combining-mark"),
        pytest.param("I \u2764\ufe0f pears", 9, id="variation-selector"),
        pytest.param(
            "\U0001f468\u200d\U0001f469\u200d\U0001f467", 6, id="zero-width-joiner"
        ),
        pytest.param("plum\u200bfig", 7, id="zero-width-space"),
        pytest.param("lime\u00adtree", 8, id="soft-hyphen"),
        pytest.param("\u6797\u6a8e", 4, id="wide"),
        pytest.param("\uff21pple", 6, id="fullwidth"),
        pytest.param("\U0001f34e apple", 8, id="emoji"),
        pytest.param("\u1100\u1161", 2, id="hangul-vowel-jamo"),
        pytest.param("Apples\t\tripen", 17, id="tab-stops"),
        pytest.param("\u6797\tpear", 9, id="tab-stop-counts-characters"),
    ),
)
def test_line_width_measures_as_ruff_does(text, width):
    """Each width is the one that Ruff `0.17.5` gives the text in an `E501`."""
    assert line_width(text) == width


@pytest.mark.parametrize(
    ("line", "overlong"),
    (
        pytest.param("# Apples ripen late.", False, id="short"),
        pytest.param("# Apples ripen in the late summer sun.", True, id="prose"),
        pytest.param("# " + "apple" * 10, False, id="single-word"),
        pytest.param("    " + "apple" * 10, False, id="bare-word"),
        pytest.param(
            "# See https://example.com/" + "orchard/" * 5, False, id="url-inside"
        ),
        pytest.param(
            "# Pears, plums, cherries and quinces https://example.com/x",
            True,
            id="url-past",
        ),
        pytest.param(
            "# SPDX-License-Identifier: GPL-2.0-or-later OR MIT",
            False,
            id="spdx-license",
        ),
        pytest.param(
            "# SPDX-FileCopyrightText: 2026 Orchard Growers",
            False,
            id="spdx-copyright",
        ),
    ),
)
def test_is_overlong_skips_what_e501_skips(line, overlong):
    """A line that `E501` skips, or that no wrap can shorten, is not reported."""
    assert is_overlong(line, LIMIT) is overlong


@pytest.mark.parametrize(
    ("source", "limit", "wrapped"),
    (
        pytest.param(
            "# Apples ripen in the late summer sun.\n# Pears follow.\nx = 1\n",
            LIMIT,
            "# Apples ripen in the late\n# summer sun.\n# Pears follow.\nx = 1\n",
            id="next-line",
        ),
        pytest.param(
            "# Step 1: wash the apples in the stream.\n# Step 2: dry them.\nx = 1\n",
            LIMIT,
            "# Step 1: wash the apples in\n# the stream.\n# Step 2: dry them.\nx = 1\n",
            id="label-lines",
        ),
        pytest.param(
            "# The apples ripen in the sun of the south.\n"
            "# TODO: pick the pears.\nx = 1\n",
            LIMIT,
            "# The apples ripen in the sun\n# of the south.\n"
            "# TODO: pick the pears.\nx = 1\n",
            id="task-comment-below",
        ),
        pytest.param(
            "# TODO(pear): ripen the apples in the late summer sun.\n"
            "# https://example.com/orchard/issues/7\nx = 1\n",
            40,
            "# TODO(pear): ripen the apples in the\n# late summer sun.\n"
            "# https://example.com/orchard/issues/7\nx = 1\n",
            id="issue-link-below",
        ),
        pytest.param(
            "# Get the list of crates one row below the cellar door\n"
            "# fruitList - the fruit to keep, any fruit found that is not in the "
            "list goes to the market, set to empty to keep all the fruit\n"
            "# returns a list of crates\ndata = 5\n",
            90,
            "# Get the list of crates one row below the cellar door\n"
            "# fruitList - the fruit to keep, any fruit found that is not in the "
            "list goes to the\n"
            "# market, set to empty to keep all the fruit\n"
            "# returns a list of crates\ndata = 5\n",
            id="autopep8-470",
        ),
        pytest.param(
            "# crate: Crate()\n"
            "# the class of the crate, fill the crate,\n"
            "# operation of moving(upward, downward, left, right) or merge, add a "
            "new apple for each step\n"
            "# weigh the crate (if the apples are ripe or not)\nx = 1\n",
            80,
            "# crate: Crate()\n"
            "# the class of the crate, fill the crate,\n"
            "# operation of moving(upward, downward, left, right) or merge, add a "
            "new apple\n"
            "# for each step\n"
            "# weigh the crate (if the apples are ripe or not)\nx = 1\n",
            id="autopep8-375",
        ),
    ),
)
def test_comment_overflow_opens_a_line_below(source, limit, wrapped):
    """The words past the limit open a new line: two comment lines never merge.

    So a label, a task tag or an issue link that opens the next line keeps it.
    """
    assert wrap_source(source, limit) == (wrapped, 1)


def test_comment_words_open_a_line_after_the_last_one():
    """Below the last line of a block, the words get a new comment line."""
    source = "x = 1\n# Plums and cherries share the same tree row.\ny = 2\n"
    wrapped, _ = wrap_source(source, LIMIT)
    assert wrapped == "x = 1\n# Plums and cherries share the\n# same tree row.\ny = 2\n"


def test_docstring_paragraph_wraps_and_its_summary_stays():
    """The summary stays one line, which `D205` asks for, however long it is."""
    source = (
        "def ripen():\n"
        '    """Ripen the fruit of the orchard row by row, slowly.\n'
        "\n"
        "    The sun warms the south side of each tree first, then the north.\n"
        '    """\n'
    )
    wrapped, shortened = wrap_source(source, LIMIT)
    assert wrapped == (
        "def ripen():\n"
        '    """Ripen the fruit of the orchard row by row, slowly.\n'
        "\n"
        "    The sun warms the south\n"
        "    side of each tree first,\n"
        "    then the north.\n"
        '    """\n'
    )
    assert shortened == 2


def test_list_item_words_hang_under_its_text():
    """The next lines of a list item start under its text, not under its marker."""
    source = _function(
        "    - Apples go to the north cellar with the pears and the quinces.\n"
        "    - Plums go south.\n"
    )
    wrapped, _ = wrap_source(source, LIMIT)
    assert wrapped == _function(
        "    - Apples go to the north\n"
        "      cellar with the pears\n"
        "      and the quinces.\n"
        "    - Plums go south.\n"
    )


def test_list_item_before_a_blank_line_hangs_under_its_text():
    """An item that ends its paragraph hangs under its text, and opens a line."""
    source = _function(
        "    - Apples go to the north cellar with the pears.\n\n    Plums go south.\n"
    )
    assert wrap_source(source, LIMIT) == (
        _function(
            "    - Apples go to the north\n"
            "      cellar with the pears.\n"
            "\n"
            "    Plums go south.\n"
        ),
        1,
    )


@pytest.mark.parametrize(
    ("marker", "hang"),
    (
        pytest.param("#.", 7, id="auto"),
        pytest.param("a.", 7, id="letter"),
        pytest.param("i)", 7, id="roman"),
        pytest.param("iv.", 8, id="roman-long"),
        pytest.param("(a)", 8, id="letter-in-parentheses"),
        pytest.param("(1)", 8, id="number-in-parentheses"),
    ),
)
def test_rest_enumerators_open_list_items(marker, hang):
    """A reST enumerator opens an item: the next item never joins the one above."""
    source = _function(
        f"    {marker} Apples go to the north cellar with the pears.\n"
        f"    {marker} Plums go south.\n"
    )
    assert wrap_source(source, LIMIT) == (
        _function(
            f"    {marker} Apples go to the north\n"
            f"{' ' * hang}cellar with the pears.\n"
            f"    {marker} Plums go south.\n"
        ),
        1,
    )


def test_field_words_hang_four_columns_past_its_name():
    """The next lines of a reST field indent by four, as the docstrings here do."""
    source = _function("    :param crate: The crate of apples to weigh.\n")
    wrapped, _ = wrap_source(source, LIMIT)
    assert wrapped == _function(
        "    :param crate: The crate of\n        apples to weigh.\n"
    )


def test_field_words_follow_the_hang_of_its_next_line():
    """A field whose next line sets its own hang keeps it, and pushes into it."""
    source = _function(
        "    :param crate: The crate of apples to weigh on the scale.\n"
        "                  It holds pears too.\n"
    )
    assert wrap_source(source, 40) == (
        _function(
            "    :param crate: The crate of apples to\n"
            "                  weigh on the scale. It\n"
            "                  holds pears too.\n"
        ),
        2,
    )


def test_comment_list_item_words_keep_the_hash_marks():
    """The next lines of a list item in a comment keep the hash marks."""
    source = "# - Apples go to the north cellar.\n# - Plums go south.\nx = 1\n"
    wrapped, _ = wrap_source(source, LIMIT)
    assert wrapped == (
        "# - Apples go to the north\n#   cellar.\n# - Plums go south.\nx = 1\n"
    )


def test_nested_comment_list_item_hangs_under_its_own_text():
    """A nested item in a comment hangs under its text, not under its parent."""
    source = (
        "# - Apples go north.\n"
        "#   - Pears go to the south cellar with the quinces.\nx = 1\n"
    )
    wrapped, shortened = wrap_source(source, 40)
    assert wrapped == (
        "# - Apples go north.\n"
        "#   - Pears go to the south cellar with\n"
        "#     the quinces.\nx = 1\n"
    )
    assert shortened == 1


def test_lines_no_wrap_can_shorten_are_left_alone():
    """Code, strings, blocks and pragmas keep their length for a human to reword."""
    source = (
        "def store():\n"
        '    """Store the crates of the orchard in the cellar, row by row.\n'
        "\n"
        "    ```python\n"
        '    crates = ["apple", "pear", "plum", "cherry", "quince"]\n'
        "    ```\n"
        "\n"
        "    >>> store_the_crates_in_the_cellar(apples=3, pears=2)\n"
        "\n"
        "        $ fruit store --crates 12 --cellar north --row 3\n"
        "\n"
        "    | Fruit | Crates in the cellar | Row |\n"
        '    """\n'
        '    label = "A crate of apples and pears for the market stall"\n'
        "    count = 3  # The number of crates on the cart today\n"
        "    # noqa: E501 the pragma comment keeps all of its words on its line\n"
        "    #     $ fruit store --crates 12 --cellar north --row 3\n"
        "    return count\n"
    )
    assert wrap_source(source, LIMIT) == (source, 0)


@pytest.mark.parametrize(
    "comment",
    (
        pytest.param(
            "# pylint: disable=too-many-arguments, too-many-locals", id="pylint"
        ),
        pytest.param("# flake8: noqa: E501, W291, W293, E203, E231", id="flake8"),
        pytest.param("# nosec B602, B607, B603, B604, B605, B606", id="nosec"),
        pytest.param("# pyrefly: ignore[missing-attribute, bad-return]", id="pyrefly"),
        pytest.param("# ty: ignore[unresolved-attribute, invalid-return]", id="ty"),
        pytest.param("# type: ignore[attr-defined, union-attr, arg-type]", id="type"),
        pytest.param("# isort: skip_file because the apples sort first", id="isort"),
        pytest.param("# ruff: noqa: E501 for the long apple names below", id="ruff"),
        pytest.param("# pyright: ignore[reportOptionalMemberAccess]", id="pyright"),
        pytest.param("# mypy: disable-error-code=attr-defined, union-attr", id="mypy"),
        pytest.param("# pragma: no cover, the pears never reach it", id="coverage"),
        pytest.param("# fmt: skip the apples, the pears and the plums", id="fmt"),
        pytest.param("# yapf: disable the apples, the pears and the plums", id="yapf"),
        pytest.param("# NOQA: E501 the apples ripen in the late summer", id="noqa"),
        pytest.param(
            "# Apples ripen in the late summer sun.  # noqa: E501", id="trailing-noqa"
        ),
    ),
)
def test_pragma_comments_keep_their_words(comment):
    """A comment that holds a pragma stays whole, at its start or after its text.

    These are the pragmas that `is_pragma_comment` of Ruff reads, and the
    comments that the formatters, mypy and coverage read.
    """
    source = f"{comment}\nx = 1\n"
    assert wrap_source(source, LIMIT) == (source, 0)


@pytest.mark.parametrize(
    "comment",
    (
        pytest.param("# Pragmatic growers pick the apples first.", id="pragma-word"),
        pytest.param("# Type: the apple variety, like Gala or Fuji.", id="type-case"),
        pytest.param("# Noah picks the apples before the frost.", id="no-noqa"),
    ),
)
def test_prose_that_opens_like_a_pragma_wraps(comment):
    """`is_pragma_comment` of Ruff reads a word before a colon, in its case."""
    source = f"{comment}\nx = 1\n"
    wrapped, shortened = wrap_source(source, LIMIT)
    assert shortened == 1
    assert wrapped != source


@pytest.mark.parametrize(
    "source",
    (
        pytest.param(
            "#!/usr/bin/env python\n"
            "# This Python file uses the following encoding: latin-1\nx = 1\n",
            id="encoding-declaration",
        ),
        pytest.param(
            "# -*- mode: python; fill-column: 79; indent-tabs-mode: nil -*-\nx = 1\n",
            id="emacs",
        ),
        pytest.param(
            "# vim: set fileencoding=utf-8 tabstop=4 shiftwidth=4 :\nx = 1\n",
            id="vim",
        ),
        pytest.param(
            "# vi: set tabstop=4 shiftwidth=4 expandtab autoindent :\nx = 1\n",
            id="vi",
        ),
        pytest.param(
            "# ex: set tabstop=4 shiftwidth=4 expandtab autoindent :\nx = 1\n",
            id="ex",
        ),
        pytest.param(
            "# cspell:ignore pearz plumz quincez cherriez figz\nx = 1\n", id="cspell"
        ),
        pytest.param(
            "# spell-checker: disable-next-line pearz plumz quincez\nx = 1\n",
            id="spell-checker",
        ),
        pytest.param(
            "# codespell:ignore pearz,plumz quincez,cherriez\nx = 1\n",
            id="codespell",
        ),
        pytest.param(
            "# %% The harvest of the apples and pears in the valley\nx = 1\n",
            id="cell-marker",
        ),
        pytest.param(
            "# TODO(pear): ripen them.\n"
            "# https://example.com/orchard/issues/7 for the pears\nx = 1\n",
            id="issue-link",
        ),
    ),
)
def test_machine_read_comments_keep_their_words(source):
    """A tool reads these lines, so their words stay where they are."""
    assert wrap_source(source, LIMIT) == (source, 0)


def test_encoding_declaration_counts_on_the_first_two_lines_only():
    """PEP 263 reads the first two lines: the same words below them are prose."""
    source = (
        "#!/usr/bin/env python\nx = 1\n"
        "# This Python file uses the following encoding: latin-1\n"
    )
    wrapped, shortened = wrap_source(source, LIMIT)
    assert wrapped == (
        "#!/usr/bin/env python\nx = 1\n"
        "# This Python file uses the\n# following encoding: latin-1\n"
    )
    assert shortened == 1


@pytest.mark.parametrize(
    "source",
    (
        pytest.param(
            "# -- Orchard rows ------------------------------\nx = 1\n", id="ruler"
        ),
        pytest.param(
            "##############################\n"
            "# Apples and pears ripen in the sun. #\n"
            "##############################\nx = 1\n",
            id="box",
        ),
        pytest.param(
            "# +--------+     +--------+     +--------+\n"
            "# | apples | --> | pears  | --> | plums  |\n"
            "# +--------+     +--------+     +--------+\nx = 1\n",
            id="ascii-art",
        ),
        pytest.param(
            "# \u250c\u2500\u2500\u2510 the apples of the north orchard\nx = 1\n",
            id="box-drawing",
        ),
        pytest.param(
            "# apple     red       round     sweet\n"
            "# lemon     yellow    oval      sour\nx = 1\n",
            id="aligned-columns",
        ),
        pytest.param(
            "# =======  =======  =======  =======\n"
            "# Fruit    Colour   Season   Cellar\n"
            "# =======  =======  =======  =======\nx = 1\n",
            id="table",
        ),
        pytest.param(
            "# Run it:\n"
            "# ```shell\n"
            "# fruit store --crates 12 --cellar north --row 3\n"
            "# ```\nx = 1\n",
            id="fence",
        ),
        pytest.param(
            "# # The harvest of the apples and pears in the valley\nx = 1\n",
            id="nested-hash",
        ),
    ),
)
def test_layout_comments_keep_their_lines(source):
    """A ruler, a box, a column or a fenced block would break if words moved."""
    assert wrap_source(source, LIMIT) == (source, 0)


def test_sentence_spacing_is_no_column():
    """Two spaces after the end of a sentence still let the line wrap."""
    source = "# The orchard gives no WARRANTY on APPLES.  See the\nx = 1\n"
    assert wrap_source(source, 45) == (
        "# The orchard gives no WARRANTY on APPLES.\n# See the\nx = 1\n",
        1,
    )


def test_commented_out_code_keeps_its_words():
    """A row of commented-out code, as `ERA001` reports it, stays whole."""
    source = (
        "# crates = sort_fruit(apples, pears, plums)\n"
        "# Apples ripen in the late summer sun.\nx = 1\n"
    )
    wrapped, shortened = wrap_source(source, LIMIT, code_rows={1})
    assert wrapped == (
        "# crates = sort_fruit(apples, pears, plums)\n"
        "# Apples ripen in the late\n# summer sun.\nx = 1\n"
    )
    assert shortened == 1


@pytest.mark.parametrize(
    "source",
    (
        pytest.param(
            "# fmt: off\n# The apples ripen in the late summer sun of the valley.\n"
            "x = [1,2]\n# fmt: on\n",
            id="fmt-off",
        ),
        pytest.param(
            "# yapf: disable\n"
            "# The apples ripen in the late summer sun of the valley.\n"
            "# yapf: enable\nx = 1\n",
            id="yapf-disable",
        ),
        pytest.param(
            "# fmt: off\n"
            "def ripen():\n"
            '    """Ripen the fruit.\n'
            "\n"
            "    The apples ripen in the late summer sun of the valley.\n"
            '    """\n'
            "# fmt: on\n",
            id="docstring-in-region",
        ),
        pytest.param(
            "x = 1\n# fmt: off\n"
            "# The apples ripen in the late summer sun of the valley.\n",
            id="region-to-the-end",
        ),
        pytest.param(
            "def ripen():\n"
            '    """Ripen the fruit.\n'
            "\n"
            "    The apples ripen in the late summer sun of the valley.\n"
            '    """  # fmt: skip\n',
            id="fmt-skip",
        ),
        pytest.param(
            "def ripen():\n"
            '    """Ripen the fruit.\n'
            "\n"
            "    The apples ripen in the late summer sun of the valley.\n"
            '    """  # noqa: E501\n',
            id="noqa",
        ),
    ),
)
def test_formatter_regions_and_docstring_pragmas_keep_their_lines(source):
    """A formatter leaves these lines as written, and a pragma covers its docstring."""
    assert wrap_source(source, LIMIT) == (source, 0)


@pytest.mark.parametrize(
    "body",
    (
        pytest.param(
            "    >>> crates()\n    ['apple', 'pear', 'plum', 'cherry', 'quince']\n",
            id="doctest-output",
        ),
        pytest.param(
            "    >>> ripen('stone')\n"
            "    Traceback (most recent call last):\n"
            "    ValueError: a stone is no fruit of the orchard\n",
            id="doctest-traceback",
        ),
        pytest.param(
            "    \\b\n"
            "    apple    red      round fruit\n"
            "    lemon    yellow   oval fruit\n",
            id="click-paragraph",
        ),
        pytest.param(
            "    .. note:: The apples ripen in the late summer sun.\n", id="directive"
        ),
        pytest.param(
            "    .. A hidden note about the pears of the cold north.\n",
            id="rest-comment",
        ),
        pytest.param(
            "    .. _the apple guide of the valley: https://example.com/apples\n",
            id="link-target",
        ),
        pytest.param(
            "    .. [1] The apples ripen in the late summer sun.\n", id="footnote"
        ),
        pytest.param(
            "    .. |apple| replace:: A round fruit of the north.\n",
            id="substitution",
        ),
        pytest.param(
            "    --crates N  The number of crates of apples on the cart.\n",
            id="long-option",
        ),
        pytest.param(
            "    -c  The number of crates of apples on the cart.\n", id="short-option"
        ),
        pytest.param(
            "    =====  ===========\n"
            "    Fruit  Description\n"
            "    =====  ===========\n"
            "    Apple  A round fruit that grows in the cool north.\n"
            "    =====  ===========\n",
            id="simple-table",
        ),
        pytest.param(
            "    +-------+------------------+\n"
            "    | Fruit | Season of harvest of the north |\n"
            "    +-------+------------------+\n",
            id="grid-table",
        ),
        pytest.param(
            "    Fruit | Season of the harvest in the valley\n    ----- | -----\n",
            id="pipe-table",
        ),
        pytest.param(
            "    The harvest of the apples in the valley\n"
            "    =======================================\n",
            id="section-title",
        ),
        pytest.param(
            "    # The harvest of the apples and pears in the valley\n",
            id="markdown-heading",
        ),
        pytest.param(
            "    [the orchard guide to apples]: https://example.com/guide\n",
            id="link-reference",
        ),
        pytest.param(
            "    crate : Crate, default: the north crate of the orchard\n",
            id="numpy-type",
        ),
        pytest.param(
            "    The apples ripen in the late summer sun \\\n    of the valley.\n",
            id="joined-line",
        ),
    ),
)
def test_docstring_markup_keeps_its_lines(body):
    """Moving words into or out of these lines would change what renders."""
    source = _function(body)
    assert wrap_source(source, LIMIT) == (source, 0)


def test_closing_line_with_code_after_the_quotes_stays():
    """The closing quotes keep the code or the comment that follows them."""
    source = (
        "def ripen():\n"
        '    """Ripen the fruit.\n'
        "\n"
        '    The apples ripen in the sun."""  # The pears follow them later.\n'
    )
    assert wrap_source(source, LIMIT) == (source, 0)


def test_numpy_section_title_stays_and_its_prose_wraps():
    """The title and its underline stay, and the prose below them wraps."""
    source = _function(
        "    Notes\n"
        "    -----\n"
        "    The apples ripen in the late summer sun of the valley.\n"
    )
    assert wrap_source(source, LIMIT) == (
        _function(
            "    Notes\n"
            "    -----\n"
            "    The apples ripen in the\n"
            "    late summer sun of the\n"
            "    valley.\n"
        ),
        2,
    )


@pytest.mark.parametrize(
    "body",
    (
        pytest.param(
            "    Args:\n"
            "        crate: The crate of apples and pears to weigh on the scale.\n",
            id="google-args",
        ),
        pytest.param(
            "    Parameters\n"
            "    ----------\n"
            "    crate : Crate\n"
            "        The crate of apples and pears to weigh on the scale.\n",
            id="numpy-description",
        ),
    ),
)
def test_sections_left_for_a_human_stay(body):
    """Google items and NumPy descriptions hang in a way the wrap does not read."""
    source = _function(body)
    assert wrap_source(source, LIMIT) == (source, 0)


@pytest.mark.parametrize(
    "token",
    (
        pytest.param(">>>", id="doctest"),
        pytest.param("...", id="ellipsis"),
        pytest.param("-", id="bullet"),
        pytest.param("1.", id="enumerator"),
        pytest.param("#.", id="auto-enumerator"),
        pytest.param("(a)", id="enumerator-in-parentheses"),
        pytest.param("..", id="rest-markup"),
        pytest.param("|", id="line-block"),
        pytest.param("#", id="hash"),
        pytest.param(":attr:`x`", id="role"),
    ),
)
def test_docstring_break_never_opens_a_line_with_markup(token):
    """The break moves one word earlier, so the next line opens on prose."""
    source = _function(f"    The apples ripen in a row {token} of the barn.\n")
    assert wrap_source(source, LIMIT) == (
        _function(f"    The apples ripen in a\n    row {token} of the barn.\n"),
        1,
    )


def test_docstring_break_never_opens_a_merged_line_with_markup():
    """The words that join the next line, with its own words, open on prose.

    A lone `-` moved to the start of `to print` would open a bullet item.
    """
    source = _function("    The cellar keeps it. Pass -\n    to print to the screen.\n")
    assert wrap_source(source, LIMIT) == (
        _function(
            "    The cellar keeps it.\n    Pass - to print to the\n    screen.\n"
        ),
        2,
    )


def test_enumerator_inside_a_sentence_is_no_list_item():
    """`500)` goes on with the sentence above: only `1)` could open a list there."""
    source = (
        "# The hooks run at a priority of 400 (vs default\n"
        "# 500) so they run first, before the others.\nx = 1\n"
    )
    wrapped, shortened = wrap_source(source, 40)
    assert wrapped == (
        "# The hooks run at a priority of 400 (vs\n"
        "# default\n"
        "# 500) so they run first, before the\n"
        "# others.\nx = 1\n"
    )
    assert shortened == 2


def test_word_of_roman_letters_is_no_enumerator():
    """`did` is no roman number, so `did.` may open the next line."""
    source = _function("    The pears fill a cart. It did. Then the plums follow.\n")
    assert wrap_source(source, LIMIT) == (
        _function(
            "    The pears fill a cart. It\n    did. Then the plums\n    follow.\n"
        ),
        2,
    )


def test_line_that_every_break_would_mark_up_stays():
    """With no break that opens a line on prose, the line keeps its words."""
    source = _function("    Rows - - - - - - - - - - - - -\n")
    assert wrap_source(source, LIMIT) == (source, 0)


@pytest.mark.parametrize(
    ("source", "wrapped"),
    (
        pytest.param(
            "# The price of the crates is 3 - 4 coins.\nx = 1\n",
            "# The price of the crates is\n# 3 - 4 coins.\nx = 1\n",
            id="list-item",
        ),
        pytest.param(
            "# The apples ripen in the row #3 of the cellar.\nx = 1\n",
            "# The apples ripen in the\n# row #3 of the cellar.\nx = 1\n",
            id="hash",
        ),
        pytest.param(
            "# The apples ripen in the row %% of the cellar.\nx = 1\n",
            "# The apples ripen in the\n# row %% of the cellar.\nx = 1\n",
            id="cell-marker",
        ),
        pytest.param(
            "# Set the crate of the apples type: Gala.\nx = 1\n",
            "# Set the crate of the\n# apples type: Gala.\nx = 1\n",
            id="type-comment",
        ),
        pytest.param(
            "# The cellar list of crates is todo work.\nx = 1\n",
            "# The cellar list of crates\n# is todo work.\nx = 1\n",
            id="task-tag",
        ),
        pytest.param(
            "# The apples ripen in the row ... of the cellar.\nx = 1\n",
            "# The apples ripen in the\n# row ... of the cellar.\nx = 1\n",
            id="ellipsis",
        ),
    ),
)
def test_comment_break_never_opens_a_line_that_a_tool_reads(source, wrapped):
    """The break moves one word earlier, so no new line reads as markup or pragma."""
    assert wrap_source(source, LIMIT) == (wrapped, 1)


@pytest.mark.parametrize(
    "source",
    (
        pytest.param(
            "if True:\n"
            "    if True:\n"
            "        if True:\n"
            "            if True:\n"
            "                if True:\n"
            "                    if True:\n"
            "                        # apples pears plums\n"
            "                        x = 1\n",
            id="comment",
        ),
        pytest.param(
            "class A:\n"
            "    class B:\n"
            "        class C:\n"
            "            class D:\n"
            "                class E:\n"
            "                    def f(self):\n"
            '                        """Ripen.\n'
            "\n"
            "                        apples pears plums\n"
            '                        """\n',
            id="docstring",
        ),
    ),
)
def test_line_whose_first_word_passes_the_limit_stays(source):
    """No break can bring such a line under the limit, so none is made."""
    assert wrap_source(source, 29) == (source, 0)


def test_named_escape_and_joined_line_no_longer_block_the_file():
    """A named escape is one word, and a line that ends with a backslash stays.

    The comment and the other docstrings of the file still wrap.
    """
    source = (
        "# The plums ripen in the late summer sun of the valley.\n"
        "'''Summary line.\n\nApple pear plum fig \\N{LATIN SMALL LETTER A} kiwi.\n'''\n"
        "\n\n"
        "def ripen():\n"
        "    '''Ripen.\n\n    Apple pear plum fig kiwi lime \\\n    mango.\n    '''\n"
    )
    wrapped, shortened = wrap_source(source, LIMIT)
    assert wrapped == (
        "# The plums ripen in the late\n# summer sun of the valley.\n"
        "'''Summary line.\n\nApple pear plum fig\n"
        "\\N{LATIN SMALL LETTER A} kiwi.\n'''\n"
        "\n\n"
        "def ripen():\n"
        "    '''Ripen.\n\n    Apple pear plum fig kiwi lime \\\n    mango.\n    '''\n"
    )
    assert shortened == 2


def test_break_never_follows_a_backslash():
    """A backslash that ends a line would join the next one in the string."""
    source = "'''Summary line.\n\nApple pear plum fig kiwi \\ lime mango.\n'''\n"
    wrapped, shortened = wrap_source(source, 26)
    assert (
        wrapped == "'''Summary line.\n\nApple pear plum fig kiwi\n\\ lime mango.\n'''\n"
    )
    assert shortened == 1


def test_docstring_whose_wrap_changes_its_string_keeps_its_lines(monkeypatch):
    """The guard compares each docstring with its wrap, and keeps the old lines.

    With named escapes split like any other word, the split breaks the string:
    only that docstring stays as it was, and the comment above it still wraps.
    """
    monkeypatch.setattr(long_lines, "_NAMED_ESCAPE", re.compile(r"(?!)"))
    source = (
        "# The plums ripen in the late summer sun of the valley.\n"
        "'''Summary line.\n\nApple pear plum fig \\N{LATIN SMALL LETTER A} kiwi.\n'''\n"
    )
    wrapped, shortened = wrap_source(source, LIMIT)
    assert wrapped == source.replace(
        "# The plums ripen in the late summer sun of the valley.\n",
        "# The plums ripen in the late\n# summer sun of the valley.\n",
    )
    assert shortened == 1


def test_string_statement_of_one_line_literals_is_left_alone():
    """A line break inside a single-quoted literal is a syntax error."""
    source = (
        "def ripen():\n"
        '    ("Apples ripen in the late sun "\n'
        '     "and pears follow them in the autumn")\n'
    )
    assert wrap_source(source, LIMIT) == (source, 0)


def test_fenced_block_closes_only_on_its_own_fence():
    """A shorter fence inside a block opens no prose: only the outer one closes it."""
    source = (
        "def store():\n"
        '    """Store the crates.\n'
        "\n"
        "    ````{note}\n"
        "    ```toml\n"
        '    cellar = "apples pears plums cherries"\n'
        "    ```\n"
        "    ````\n"
        "\n"
        "    The cellar keeps the crates of apples cool.\n"
        '    """\n'
    )
    wrapped, shortened = wrap_source(source, LIMIT)
    assert wrapped == source.replace(
        "    The cellar keeps the crates of apples cool.\n",
        "    The cellar keeps the\n    crates of apples cool.\n",
    )
    assert shortened == 1


@pytest.mark.parametrize("limit", (29, 30))
def test_spaces_at_the_break_stay_on_neither_line(limit):
    """A run of spaces where the line breaks ends no line, and opens none."""
    source = "# Apples ripen in the summer.  Pears follow them.\nx = 1\n"
    assert wrap_source(source, limit) == (
        "# Apples ripen in the summer.\n# Pears follow them.\nx = 1\n",
        1,
    )


def test_only_the_rows_ruff_reports_wrap():
    """With the rows of Ruff, a long line that Ruff leaves out stays as it is."""
    source = (
        "# Apples ripen in the late summer sun.\n"
        "# Pears ripen in the early autumn rain.\nx = 1\n"
    )
    wrapped, shortened = wrap_source(source, LIMIT, {2: LIMIT})
    assert wrapped == (
        "# Apples ripen in the late summer sun.\n"
        "# Pears ripen in the early\n# autumn rain.\nx = 1\n"
    )
    assert shortened == 1


def test_reported_code_line_stays():
    """A row of code that Ruff reports is no comment or docstring: it stays."""
    source = (
        'label = "A crate of apples and pears for the market stall"\n'
        "# Apples ripen in the late summer sun.\n"
    )
    wrapped, shortened = wrap_source(source, LIMIT, {1: LIMIT, 2: LIMIT})
    assert wrapped == (
        'label = "A crate of apples and pears for the market stall"\n'
        "# Apples ripen in the late\n# summer sun.\n"
    )
    assert shortened == 1


def test_each_row_wraps_to_the_limit_of_its_message():
    """`E501` and `W505` can give a comment and a docstring different limits."""
    source = (
        "# Apples ripen in the late summer sun.\n"
        "def ripen():\n"
        '    """Ripen the fruit.\n'
        "\n"
        "    The pears ripen in the early autumn rain.\n"
        '    """\n'
    )
    wrapped, shortened = wrap_source(source, 40, {1: 25, 5: 40})
    assert wrapped == (
        "# Apples ripen in the\n# late summer sun.\n"
        "def ripen():\n"
        '    """Ripen the fruit.\n'
        "\n"
        "    The pears ripen in the early autumn\n"
        "    rain.\n"
        '    """\n'
    )
    assert shortened == 2


@pytest.mark.parametrize(
    "source",
    (
        pytest.param(
            "# Apples ripen in the late summer sun, then the pears and plums.\nx = 1\n",
            id="comment",
        ),
        pytest.param(
            "# - Apples go to the north cellar.\n#   - Pears go to the south cellar.\n",
            id="comment-list",
        ),
        pytest.param(
            _function(
                "    The sun warms the south side of each tree first, then the north.\n"
                "    The rain follows it in the autumn.\n"
            ),
            id="docstring",
        ),
        pytest.param(
            _function(
                "    #. Apples go to the north cellar with the pears.\n"
                "    #. Plums go to the south cellar with the cherries.\n"
            ),
            id="enumerators",
        ),
        pytest.param(
            _function(
                "    :param crate: The crate of apples to weigh on the scale.\n"
                "        It holds pears too, and plums from the south.\n"
            ),
            id="field",
        ),
        pytest.param(
            _function(
                "    The apples ripen in a row >>> of the cellar and the barn.\n"
            ),
            id="never-open",
        ),
    ),
)
def test_wrap_is_idempotent(source):
    """A second run finds nothing left to wrap."""
    once, shortened = wrap_source(source, LIMIT)
    assert shortened
    assert wrap_source(once, LIMIT) == (once, 0)


def _finding(code: str, path: Path, row: int, message: str = "") -> dict:
    """Shape one finding as the JSON output of Ruff does."""
    return {
        "code": code,
        "filename": str(path.resolve()),
        "location": {"row": row, "column": 1},
        "message": message,
    }


@dataclass
class FakeRuff:
    """Stand in for the Ruff that `run_tool` starts, reading the files on disk.

    It reports `E501` on each line that runs past *limit*, `W505` on each line
    past *doc_limit* when it is set, both measured with *tab_size*, and each rule
    of *rules* on each line its test accepts, with the text of the file.
    """

    limit: int = LIMIT
    doc_limit: int | None = None
    tab_size: int = 4
    rules: dict[str, Callable[[str, str], bool]] = field(default_factory=dict)
    exit_codes: list[int] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    seen: list[dict[str, str]] = field(default_factory=list)
    """The text of each file, as each call read it from disk."""

    def __call__(self, name: str, extra_args: Sequence[str] = ()) -> int:
        assert name == "ruff"
        args = list(extra_args)
        self.calls.append(args)
        if self.exit_codes:
            exit_code = self.exit_codes.pop(0)
            if exit_code:
                return exit_code
        findings = []
        self.seen.append({})
        for target in args[args.index("--") + 1 :]:
            path = Path(target)
            text = path.read_bytes().decode("UTF-8", errors="replace")
            self.seen[-1][target] = text
            for row, line in enumerate(text.split("\n"), start=1):
                width = line_width(line, self.tab_size)
                if is_overlong(line, self.limit, self.tab_size):
                    message = f"Line too long ({width} > {self.limit})"
                    findings.append(_finding("E501", path, row, message))
                if self.doc_limit and is_overlong(line, self.doc_limit, self.tab_size):
                    message = f"Doc line too long ({width} > {self.doc_limit})"
                    findings.append(_finding("W505", path, row, message))
                findings.extend(
                    _finding(code, path, row)
                    for code, fires in self.rules.items()
                    if fires(line, text)
                )
        report = Path(args[args.index("--output-file") + 1])
        report.write_text(json.dumps(findings), encoding="UTF-8")
        return 0


@pytest.fixture
def fake_ruff(monkeypatch) -> FakeRuff:
    """Replace the Ruff that `fix_long_lines` runs with a {class}`FakeRuff`."""
    ruff = FakeRuff()
    monkeypatch.setattr(long_lines, "run_tool", ruff)
    return ruff


def test_fix_long_lines_checks_with_ruff_then_checks_again(tmp_path, fake_ruff):
    """Ruff picks the rows, then checks the file again with the same flags."""
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"# Apples ripen in the late summer sun.\nx = 1\n")
    assert fix_long_lines([orchard]) == {orchard: 1}
    assert orchard.read_bytes() == b"# Apples ripen in the late\n# summer sun.\nx = 1\n"
    assert len(fake_ruff.calls) == 2
    first, second = fake_ruff.calls
    assert first[: first.index("--output-file")] == [
        "check",
        "--no-cache",
        "--no-fix",
        "--exit-zero",
        "--output-format",
        "json",
        "--extend-select",
        "W505,ERA001",
    ]
    assert first[first.index("--") + 1 :] == [str(orchard)]
    assert (
        second[: second.index("--output-file")] == first[: first.index("--output-file")]
    )


def test_line_length_adds_e501_at_that_limit(tmp_path, fake_ruff):
    """`--line-length` checks `E501` at its width, whatever the project selects."""
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"x = 1\n")
    fix_long_lines([orchard], line_length=40)
    (call,) = fake_ruff.calls
    assert call[call.index("--extend-select") + 1] == "W505,ERA001,E501"
    assert call[call.index("--config") + 1] == "lint.pycodestyle.max-line-length = 40"


def test_default_paths_are_the_python_sources(tmp_path, monkeypatch, fake_ruff):
    """With no path, the run takes the Python sources below the working directory."""
    (tmp_path / "orchard.py").write_bytes(
        b"# Apples ripen in the late summer sun.\nx = 1\n"
    )
    (tmp_path / "notes.txt").write_bytes(b"# Apples ripen in the late summer sun.\n")
    monkeypatch.chdir(tmp_path)
    assert fix_long_lines() == {Path("orchard.py"): 1}
    first, _ = fake_ruff.calls
    assert first[first.index("--") + 1 :] == ["orchard.py"]


def test_no_python_source_starts_no_ruff(tmp_path, monkeypatch, fake_ruff):
    """A repository with no Python source has nothing for Ruff to check."""
    monkeypatch.chdir(tmp_path)
    assert fix_long_lines() == {}
    assert fake_ruff.calls == []


def test_long_list_of_paths_goes_to_ruff_in_batches(tmp_path, monkeypatch, fake_ruff):
    """Each Ruff command line takes as many paths as the budget allows."""
    monkeypatch.setattr(long_lines, "ARGUMENT_BUDGET", 1)
    paths = []
    for name in ("apple", "pear"):
        path = tmp_path / f"{name}.py"
        path.write_bytes(b"# Apples ripen in the late summer sun.\nx = 1\n")
        paths.append(path)
    assert fix_long_lines(paths) == dict.fromkeys(paths, 1)
    batches = [call[call.index("--") + 1 :] for call in fake_ruff.calls]
    assert batches == [[str(path)] for path in paths] * 2


def test_tab_width_follows_the_width_that_ruff_reports(tmp_path, fake_ruff):
    """A tab runs to the stop of the `indent-width` that the `E501` width shows."""
    fake_ruff.tab_size = 8
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"# Apples\tripen in the late summer sun.\nx = 1\n")
    assert fix_long_lines([orchard]) == {orchard: 1}
    assert (
        orchard.read_bytes() == b"# Apples\tripen in the\n# late summer sun.\nx = 1\n"
    )


def test_smaller_limit_of_a_row_wins(tmp_path, fake_ruff):
    """A row that `E501` and `W505` both report wraps to the smaller limit.

    The commented-out code that `ERA001` reports in the same block stays.
    """
    fake_ruff.limit = 40
    fake_ruff.doc_limit = 25
    fake_ruff.rules["ERA001"] = lambda line, _: line.startswith("# crates =")
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(
        b"# Apples ripen in the late summer sun of the valley.\n"
        b"# crates = sort_fruit(apples, pears, plums)\nx = 1\n"
    )
    assert fix_long_lines([orchard]) == {orchard: 2}
    assert orchard.read_bytes() == (
        b"# Apples ripen in the\n# late summer sun of the\n# valley.\n"
        b"# crates = sort_fruit(apples, pears, plums)\nx = 1\n"
    )


def test_block_guard_keeps_a_line_that_a_push_leaves_too_long(
    tmp_path, fake_ruff, monkeypatch, caplog
):
    """A block whose wrap leaves a line past the limit keeps its old lines."""
    original = long_lines._break

    def break_once(line, limit, tab_size, rest):
        if line.origin is None:
            return None
        return original(line, limit, tab_size, rest)

    monkeypatch.setattr(long_lines, "_break", break_once)
    orchard = tmp_path / "orchard.py"
    content = (
        b"# Apples ripen in the late summer sun of the valley, then the pears.\nx = 1\n"
    )
    orchard.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {}
    assert orchard.read_bytes() == content
    assert (
        f"{orchard}:1: left as it is: a wrapped line would still be too long"
        in caplog.text
    )


def test_file_guard_keeps_the_code_that_the_block_guard_misses(
    tmp_path, fake_ruff, monkeypatch, caplog
):
    """The last guard parses the whole file, and keeps every block when it fails."""
    monkeypatch.setattr(long_lines, "_NAMED_ESCAPE", re.compile(r"(?!)"))
    monkeypatch.setattr(long_lines, "_string_value", lambda *_: "")
    orchard = tmp_path / "orchard.py"
    content = (
        b"# The plums ripen in the late summer sun of the valley.\n"
        b"'''Summary line.\n\n"
        b"Apple pear plum fig \\N{LATIN SMALL LETTER A} kiwi.\n'''\n"
    )
    orchard.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {}
    assert orchard.read_bytes() == content
    for row in (1, 2):
        assert (
            f"{orchard}:{row}: left as it is: the wrap would change the code"
            in caplog.text
        )


def test_ast_guard_checks_each_text_before_it_is_written(
    tmp_path, fake_ruff, monkeypatch
):
    """Every text that reaches the disk was parsed against the source first.

    The first block goes back after the first check, so the file gets two writes.
    """
    checked = []
    real = long_lines._keeps_code

    def spy(source, wrapped):
        checked.append(wrapped)
        return real(source, wrapped)

    monkeypatch.setattr(long_lines, "_keeps_code", spy)
    fake_ruff.rules["TD999"] = lambda line, _: line == "# summer sun."
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(
        b"# Apples ripen in the late summer sun.\nx = 1\n"
        b"# Pears ripen in the early autumn rain.\ny = 2\n"
    )
    assert fix_long_lines([orchard]) == {orchard: 1}
    written = [seen[str(orchard)] for seen in fake_ruff.seen[1:]]
    assert len(written) == 2
    assert all(text in checked for text in written)


def test_text_that_would_change_the_code_is_never_written(
    tmp_path, fake_ruff, monkeypatch, caplog
):
    """A text that fails the guard at its write leaves the file as it was."""
    real_plan, real_keeps = long_lines._plan, long_lines._keeps_code
    planned = []

    def plan(*args):
        result = real_plan(*args)
        planned.append(result)
        return result

    monkeypatch.setattr(long_lines, "_plan", plan)
    monkeypatch.setattr(
        long_lines,
        "_keeps_code",
        lambda source, wrapped: not planned and real_keeps(source, wrapped),
    )
    orchard = tmp_path / "orchard.py"
    content = b"# Apples ripen in the late summer sun.\nx = 1\n"
    orchard.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {}
    assert orchard.read_bytes() == content
    assert len(fake_ruff.calls) == 1
    assert f"Leaving {orchard} as it is: the wrap would change its code" in caplog.text


def test_failure_restores_every_file_the_run_wrote(tmp_path, fake_ruff):
    """Ruff that fails on a later check also restores a file an earlier one settled."""
    fake_ruff.rules["TD999"] = lambda line, _: line == "# summer sun."
    fake_ruff.exit_codes = [0, 0, 2]
    settled = tmp_path / "settled.py"
    settled_content = b"# Pears ripen in the early autumn rain.\nx = 1\n"
    settled.write_bytes(settled_content)
    flagged = tmp_path / "flagged.py"
    flagged_content = (
        b"# Apples ripen in the late summer sun.\nx = 1\n"
        b"# Plums ripen in the late autumn rain.\ny = 2\n"
    )
    flagged.write_bytes(flagged_content)
    with pytest.raises(RuffError, match="Ruff exited with code 2"):
        fix_long_lines([settled, flagged])
    assert len(fake_ruff.calls) == 3
    assert settled.read_bytes() == settled_content
    assert flagged.read_bytes() == flagged_content


def test_recheck_stops_at_its_cap_and_restores_the_file(
    tmp_path, fake_ruff, monkeypatch, caplog
):
    """A file that still gains a finding at the last check goes back whole."""
    monkeypatch.setattr(long_lines, "RECHECK_PASSES", 2)

    def first_overflow(line, text):
        overflows = [row for row in text.split("\n") if row.startswith("# summer ")]
        return bool(overflows) and line == overflows[0]

    fake_ruff.rules["TD999"] = first_overflow
    orchard = tmp_path / "orchard.py"
    content = (
        b"# Apples ripen in the late summer sun.\nx = 1\n"
        b"# Pears ripen in the late summer rain.\ny = 2\n"
        b"# Plums ripen in the late summer heat.\nz = 3\n"
    )
    orchard.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {}
    assert orchard.read_bytes() == content
    assert len(fake_ruff.calls) == 3
    assert (
        f"Leaving {orchard} as it is: Ruff still finds a new problem in it after 2 "
        "checks" in caplog.text
    )


def test_every_check_reads_no_cache_and_reports_outside_the_tree(
    tmp_path, monkeypatch, fake_ruff
):
    """Each Ruff call skips the cache, and its JSON report never lands in the tree."""
    fake_ruff.rules["TD999"] = lambda line, _: line == "# summer sun."
    project = tmp_path / "orchard"
    project.mkdir()
    (project / "orchard.py").write_bytes(
        b"# Apples ripen in the late summer sun.\nx = 1\n"
        b"# Pears ripen in the early autumn rain.\ny = 2\n"
    )
    monkeypatch.chdir(project)
    assert fix_long_lines([Path("orchard.py")]) == {Path("orchard.py"): 1}
    assert len(fake_ruff.calls) == 3
    for call in fake_ruff.calls:
        assert "--no-cache" in call
        report = Path(call[call.index("--output-file") + 1])
        assert not report.resolve().is_relative_to(project.resolve())
        assert not report.exists()
    assert [path.name for path in project.iterdir()] == ["orchard.py"]


def test_file_that_ruff_leaves_out_stays(tmp_path, fake_ruff):
    """A long line that Ruff does not report, like one its config ignores, stays."""
    orchard = tmp_path / "orchard.py"
    orchard.write_text(f"{LONG_COMMENT}\nx = 1\n", encoding="UTF-8")
    fake_ruff.limit = 200
    assert fix_long_lines([orchard]) == {}
    assert orchard.read_text(encoding="UTF-8") == f"{LONG_COMMENT}\nx = 1\n"
    assert len(fake_ruff.calls) == 1


def test_block_with_a_new_finding_gets_its_lines_back(tmp_path, fake_ruff, caplog):
    """Only the block that Ruff flags after the wrap goes back to how it was."""
    fake_ruff.rules["TD999"] = lambda line, _: line == "# summer sun."
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(
        b"# Apples ripen in the late summer sun.\nx = 1\n"
        b"# Pears ripen in the early autumn rain.\ny = 2\n"
    )
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {orchard: 1}
    assert orchard.read_bytes() == (
        b"# Apples ripen in the late summer sun.\nx = 1\n"
        b"# Pears ripen in the early\n# autumn rain.\ny = 2\n"
    )
    assert (
        f"{orchard}:1: left as it is: Ruff reports TD999 on it after the wrap"
        in caplog.text
    )
    assert len(fake_ruff.calls) == 3


def test_new_finding_outside_the_blocks_goes_to_the_nearest(
    tmp_path, fake_ruff, caplog
):
    """A finding on a line that no wrap touched blames the block nearest to it."""
    fake_ruff.rules["RUF999"] = lambda line, text: (
        line == "y = 2" and "# autumn rain." in text
    )
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(
        b"# Apples ripen in the late summer sun.\nx = 1\n"
        b"# Pears ripen in the early autumn rain.\ny = 2\n"
    )
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {orchard: 1}
    assert orchard.read_bytes() == (
        b"# Apples ripen in the late\n# summer sun.\nx = 1\n"
        b"# Pears ripen in the early autumn rain.\ny = 2\n"
    )
    assert (
        f"{orchard}:4: left as it is: Ruff reports RUF999 on line 5 after the wrap"
        in caplog.text
    )


def test_every_block_flagged_leaves_the_file_as_it_was(tmp_path, fake_ruff):
    """A file whose last block goes back is written as it was, byte for byte."""
    fake_ruff.rules["TD999"] = lambda line, _: line == "# summer sun."
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"# Apples ripen in the late summer sun.\nx = 1\n")
    assert fix_long_lines([orchard], line_length=LIMIT) == {}
    assert orchard.read_bytes() == b"# Apples ripen in the late summer sun.\nx = 1\n"
    assert len(fake_ruff.calls) == 2


@pytest.mark.parametrize(
    "exit_codes",
    (pytest.param([2], id="first-check"), pytest.param([0, 2], id="second-check")),
)
def test_ruff_failure_stops_the_run_and_keeps_the_files(
    tmp_path, fake_ruff, exit_codes
):
    """Ruff that cannot finish raises, and every file holds its old content."""
    fake_ruff.exit_codes = exit_codes
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"# Apples ripen in the late summer sun.\nx = 1\n")
    with pytest.raises(RuffError, match="Ruff exited with code 2"):
        fix_long_lines([orchard])
    assert orchard.read_bytes() == b"# Apples ripen in the late summer sun.\nx = 1\n"


@pytest.mark.parametrize(
    ("content", "wrapped"),
    (
        pytest.param(
            b"\xef\xbb\xbf# Apples ripen in the late summer sun.\nx = 1\n",
            b"\xef\xbb\xbf# Apples ripen in the late\n# summer sun.\nx = 1\n",
            id="utf-8-byte-order-mark",
        ),
        pytest.param(
            f"# -*- coding: latin-1 -*-\n# {FRENCH}\nx = 1\n".encode("latin-1"),
            f"# -*- coding: latin-1 -*-\n# {FRENCH_WRAPPED}\nx = 1\n".encode("latin-1"),
            id="latin-1-cookie",
        ),
    ),
)
def test_fix_long_lines_keeps_the_encoding_of_the_file(
    tmp_path, fake_ruff, content, wrapped
):
    """The file decodes by its PEP 263 cookie, and keeps its encoding and its mark."""
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(content)
    assert fix_long_lines([orchard]) == {orchard: 1}
    assert orchard.read_bytes() == wrapped


def test_file_that_does_not_decode_is_left_and_the_run_goes_on(
    tmp_path, fake_ruff, caplog
):
    """Bytes that are no UTF-8, with no cookie, get a warning, not a crash."""
    broken = tmp_path / "broken.py"
    broken.write_bytes(f"# {FRENCH}\nx = 1\n".encode("latin-1"))
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"# Apples ripen in the late summer sun.\nx = 1\n")
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([broken, orchard]) == {orchard: 1}
    assert f"Leaving {broken} as it is: it does not decode" in caplog.text


def test_fix_long_lines_leaves_a_file_that_does_not_parse(tmp_path, fake_ruff, caplog):
    """A file that is not Python stays as it is, and the run goes on."""
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"# Apples ripen in the late summer sun.\nx = 1\n")
    broken = tmp_path / "broken.py"
    broken.write_bytes(b"# Apples ripen in the late summer sun.\ndef (:\n")
    with caplog.at_level(logging.WARNING):
        report = fix_long_lines([orchard, broken])
    assert report == {orchard: 1}
    assert orchard.read_bytes() == b"# Apples ripen in the late\n# summer sun.\nx = 1\n"
    assert broken.read_bytes() == b"# Apples ripen in the late summer sun.\ndef (:\n"
    assert "does not parse" in caplog.text


@pytest.mark.parametrize(
    ("content", "ending"),
    (
        pytest.param(
            b"# Apples ripen in the late summer sun.\r\nx = 1\r\n", "CRLF", id="crlf"
        ),
        pytest.param(b"# Apples ripen in the late summer sun.\rx = 1\r", "CR", id="cr"),
    ),
)
def test_fix_long_lines_names_the_line_endings_it_leaves(
    tmp_path, fake_ruff, caplog, content, ending
):
    """A rewrite would turn its line endings, so the file is left alone."""
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {}
    assert orchard.read_bytes() == content
    assert f"it has {ending} line endings" in caplog.text


def test_fix_long_lines_warns_on_the_docstring_it_keeps(
    tmp_path, fake_ruff, monkeypatch, caplog
):
    """The guard names the file and the first line of the docstring it keeps."""
    monkeypatch.setattr(long_lines, "_NAMED_ESCAPE", re.compile(r"(?!)"))
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(
        b"x = 1\n'''Summary line.\n\n"
        b"Apple pear plum fig \\N{LATIN SMALL LETTER A} kiwi.\n'''\n"
    )
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {}
    assert (
        f"{orchard}:2: left as it is: the wrap would change the string" in caplog.text
    )


def test_fix_long_lines_command_names_each_file(tmp_path, monkeypatch, fake_ruff):
    """The command reports each file it rewrote, with its count of lines."""
    (tmp_path / "orchard.py").write_bytes(
        b"# Apples ripen in the late summer sun.\nx = 1\n"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(repomatic, ["fix-long-lines", "orchard.py"])
    assert result.exit_code == 0, result.output
    assert "orchard.py: wrapped 1 line(s)" in result.output


@pytest.mark.parametrize(
    "error",
    (
        pytest.param(RuffError("Ruff exited with code 2."), id="ruff"),
        pytest.param(ToolInstallError("uvx is missing"), id="install"),
    ),
)
def test_fix_long_lines_command_fails_when_ruff_cannot_run(
    tmp_path, monkeypatch, error
):
    """No line can be picked without Ruff, so the command fails and says why."""

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(long_lines, "run_tool", refuse)
    (tmp_path / "orchard.py").write_bytes(b"x = 1\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(repomatic, ["fix-long-lines", "orchard.py"])
    assert result.exit_code == 1
    assert str(error) in result.output


def _project(root: Path, pyproject: str, **sources: str) -> list[Path]:
    """Write a project of *sources* under *root*, with its `pyproject.toml`."""
    (root / "pyproject.toml").write_text(pyproject, encoding="UTF-8")
    paths = []
    for name, text in sources.items():
        path = root / f"{name}.py"
        path.write_bytes(text.encode("UTF-8"))
        paths.append(path)
    return paths


@pytest.mark.once
@pytest.mark.real_http
def test_line_width_matches_ruff(tmp_path, monkeypatch):
    """The width of each fixture of the width test is the one Ruff reports."""
    skip_unless_tool_runs("ruff")
    texts = [
        "Pe\u0301che",
        "I \u2764\ufe0f pears",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",
        "lime\u00adtree",
        "\u6797\u6a8e",
        "\U0001f34e apple",
        "\u1100\u1161",
        "Apples\t\tripen",
        "\u6797\tpear",
    ]
    lines = [f"x{index} = 1  # {text}" for index, text in enumerate(texts)]
    (tmp_path / "widths.py").write_text("\n".join(lines) + "\n", encoding="UTF-8")
    report = tmp_path / "widths.json"
    monkeypatch.chdir(tmp_path)
    assert (
        run_tool(
            "ruff",
            [
                "check",
                "--no-cache",
                "--no-fix",
                "--exit-zero",
                "--select",
                "E501",
                "--line-length",
                "1",
                "--output-format",
                "json",
                "--output-file",
                str(report),
                "--",
                "widths.py",
            ],
        )
        == 0
    )
    widths = {
        item["location"]["row"]: int(item["message"].split("(")[1].split(" ")[0])
        for item in json.loads(report.read_text(encoding="UTF-8"))
    }
    assert widths == {row: line_width(line) for row, line in enumerate(lines, start=1)}


@pytest.mark.once
@pytest.mark.real_http
def test_ruff_picks_the_lines_and_the_limits(tmp_path, monkeypatch, caplog):
    """Ruff's own settings choose each line and its limit, and its check holds.

    `max-doc-length` wraps the comments and the docstrings at 30. A `noqa`
    behind a docstring, a `ruff: disable` range, `per-file-ignores` and
    commented-out code keep their lines. The link under a task comment stays on
    its own line, so `TD003` still finds it. A wrap that would add an `ERA001`
    finding goes back, with a warning.
    """
    skip_unless_tool_runs("ruff")
    orchard, skipped = _project(
        tmp_path,
        "[tool.ruff]\nline-length = 40\n"
        '[tool.ruff.lint]\nselect = ["E501", "W505", "TD003", "ERA001", "RUF100"]\n'
        "[tool.ruff.lint.pycodestyle]\nmax-doc-length = 30\n"
        '[tool.ruff.lint.per-file-ignores]\n"skipped.py" = ["E501", "W505"]\n',
        orchard=(
            "# The apples ripen in the late summer sun of the valley.\n"
            "# TODO(pear): ripen the plums in the summer.\n"
            "# https://example.com/orchard/issues/7\n"
            "x = 1\n"
            "# crates = sort_fruit(apples, pears, plums, cherries)\n"
            "y = 2\n"
            "# ruff: disable[E501, W505]\n"
            "# The cherries ripen in the early summer rain.\n"
            "# ruff: enable[E501, W505]\n"
            "z = 3\n"
            "# Set the size of the cart: crates = 12\n"
            "w = 4\n"
            "\n"
            "\n"
            "def ripen():\n"
            '    """Ripen the fruit.\n'
            "\n"
            "    The pears follow the apples in the autumn rain.\n"
            '    """\n'
            "\n"
            "\n"
            "def keep():\n"
            '    """Keep the fruit.\n'
            "\n"
            "    The plums stay in the cellar of the north orchard.\n"
            '    """  # noqa: E501, W505\n'
        ),
        skipped="# The apples ripen in the late summer sun of the valley.\nx = 1\n",
    )
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING):
        report = fix_long_lines([orchard, skipped])
    assert report == {orchard: 3}
    assert orchard.read_text(encoding="UTF-8") == (
        "# The apples ripen in the late\n"
        "# summer sun of the valley.\n"
        "# TODO(pear): ripen the plums\n"
        "# in the summer.\n"
        "# https://example.com/orchard/issues/7\n"
        "x = 1\n"
        "# crates = sort_fruit(apples, pears, plums, cherries)\n"
        "y = 2\n"
        "# ruff: disable[E501, W505]\n"
        "# The cherries ripen in the early summer rain.\n"
        "# ruff: enable[E501, W505]\n"
        "z = 3\n"
        "# Set the size of the cart: crates = 12\n"
        "w = 4\n"
        "\n"
        "\n"
        "def ripen():\n"
        '    """Ripen the fruit.\n'
        "\n"
        "    The pears follow the\n"
        "    apples in the autumn rain.\n"
        '    """\n'
        "\n"
        "\n"
        "def keep():\n"
        '    """Keep the fruit.\n'
        "\n"
        "    The plums stay in the cellar of the north orchard.\n"
        '    """  # noqa: E501, W505\n'
    )
    assert skipped.read_text(encoding="UTF-8") == (
        "# The apples ripen in the late summer sun of the valley.\nx = 1\n"
    )
    assert f"{orchard}:13: left as it is: Ruff reports ERA001" in caplog.text


@pytest.mark.once
@pytest.mark.real_http
def test_wrap_that_ruff_flags_keeps_its_lines(tmp_path, monkeypatch, caplog):
    """A wrap that makes a comment read as code gets its old lines back."""
    skip_unless_tool_runs("ruff")
    (orchard,) = _project(
        tmp_path,
        "[tool.ruff]\nline-length = 40\n"
        '[tool.ruff.lint]\nselect = ["E501", "ERA001"]\n',
        orchard="# Set the size of the cart in the settings: crates = 12\nx = 1\n",
    )
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard]) == {}
    assert orchard.read_text(encoding="UTF-8") == (
        "# Set the size of the cart in the settings: crates = 12\nx = 1\n"
    )
    assert "Ruff reports ERA001 on it after the wrap" in caplog.text


@pytest.mark.once
@pytest.mark.real_http
def test_line_length_option_adds_e501(tmp_path, monkeypatch):
    """A selection without `E501` wraps nothing, unless the limit is given."""
    skip_unless_tool_runs("ruff")
    (orchard,) = _project(
        tmp_path,
        '[tool.ruff.lint]\nselect = ["F"]\n',
        orchard=f"{LONG_COMMENT}\nx = 1\n",
    )
    monkeypatch.chdir(tmp_path)
    assert fix_long_lines([orchard]) == {}
    assert fix_long_lines([orchard], line_length=LIMIT) == {orchard: 3}
    assert orchard.read_text(encoding="UTF-8") == (
        "# The apples ripen in the late\n# summer sun of the valley,\n"
        "# then the pears follow them\n# in the autumn rain.\nx = 1\n"
    )


@pytest.mark.once
@pytest.mark.real_http
def test_bundled_config_applies_to_a_project_with_none(tmp_path, monkeypatch):
    """With no Ruff configuration, the bundled default checks `E501` at 88."""
    skip_unless_tool_runs("ruff")
    orchard = tmp_path / "orchard.py"
    orchard.write_text(f"{LONG_COMMENT}\nx = 1\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    assert fix_long_lines([orchard]) == {orchard: 1}
    assert orchard.read_text(encoding="UTF-8") == (
        "# The apples ripen in the late summer sun of the valley, then the pears"
        " follow them in\n# the autumn rain.\nx = 1\n"
    )
