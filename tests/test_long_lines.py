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

import logging

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.long_lines import (
    DEFAULT_LINE_LENGTH,
    fix_long_lines,
    is_overlong,
    ruff_line_length,
    wrap_source,
)

LIMIT = 30
"""A short limit, so each case stays readable."""


@pytest.mark.parametrize(
    ("line", "overlong"),
    (
        pytest.param("# Apples ripen late.", False, id="short"),
        pytest.param("# Apples ripen in the late summer sun.", True, id="prose"),
        pytest.param("# " + "apple" * 10, False, id="single-word"),
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


def test_comment_words_move_into_the_next_line_of_the_block():
    """The words past the limit open the next line, whose break stays."""
    source = "# Apples ripen in the late summer sun.\n# Pears follow.\nx = 1\n"
    wrapped, shortened = wrap_source(source, LIMIT)
    assert wrapped == "# Apples ripen in the late\n# summer sun. Pears follow.\nx = 1\n"
    assert shortened == 1


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
    source = (
        "def sort():\n"
        '    """Sort the fruit.\n'
        "\n"
        "    - Apples go to the north cellar with the pears and the quinces.\n"
        "    - Plums go south.\n"
        '    """\n'
    )
    wrapped, _ = wrap_source(source, LIMIT)
    assert wrapped == (
        "def sort():\n"
        '    """Sort the fruit.\n'
        "\n"
        "    - Apples go to the north\n"
        "      cellar with the pears\n"
        "      and the quinces.\n"
        "    - Plums go south.\n"
        '    """\n'
    )


def test_field_words_hang_four_columns_past_its_name():
    """The next lines of a reST field indent by four, as the docstrings here do."""
    source = (
        "def weigh(crate):\n"
        '    """Weigh a crate.\n'
        "\n"
        "    :param crate: The crate of apples to weigh.\n"
        '    """\n'
    )
    wrapped, _ = wrap_source(source, LIMIT)
    assert wrapped == (
        "def weigh(crate):\n"
        '    """Weigh a crate.\n'
        "\n"
        "    :param crate: The crate of\n"
        "        apples to weigh.\n"
        '    """\n'
    )


def test_comment_list_item_words_keep_the_hash_marks():
    """The next lines of a list item in a comment keep the hash marks."""
    source = "# - Apples go to the north cellar.\n# - Plums go south.\nx = 1\n"
    wrapped, _ = wrap_source(source, LIMIT)
    assert wrapped == (
        "# - Apples go to the north\n#   cellar.\n# - Plums go south.\nx = 1\n"
    )


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
        pytest.param(
            "# Apples ripen in the late summer sun.  # noqa: E501", id="trailing-noqa"
        ),
    ),
)
def test_pragma_comments_keep_their_words(comment):
    """A comment that holds a pragma stays whole, at its start or after its text.

    These are the pragmas that `E501` leaves out when it measures a line.
    """
    source = f"{comment}\nx = 1\n"
    assert wrap_source(source, LIMIT) == (source, 0)


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


def test_wrap_is_idempotent():
    """A second run finds nothing left to wrap."""
    source = "# Apples ripen in the late summer sun, then the pears and plums.\nx = 1\n"
    once, _ = wrap_source(source, LIMIT)
    assert wrap_source(once, LIMIT) == (once, 0)


@pytest.mark.parametrize(
    ("files", "expected"),
    (
        pytest.param({}, DEFAULT_LINE_LENGTH, id="no-config"),
        pytest.param(
            {"pyproject.toml": "[tool.ruff]\nline-length = 100\n"}, 100, id="pyproject"
        ),
        pytest.param(
            {
                "pyproject.toml": "[tool.ruff]\nline-length = 100\n",
                "ruff.toml": "line-length = 120\n",
            },
            120,
            id="ruff-toml-wins",
        ),
        pytest.param(
            {"ruff.toml": "line-length = 120\n", ".ruff.toml": "line-length = 79\n"},
            79,
            id="dot-ruff-toml-wins",
        ),
    ),
)
def test_ruff_line_length_reads_the_file_ruff_reads(tmp_path, files, expected):
    """The limit is the one that Ruff's own configuration search finds."""
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="UTF-8")
    assert ruff_line_length(tmp_path) == expected


def test_fix_long_lines_leaves_a_file_that_does_not_parse(tmp_path, caplog):
    """A file that is not Python stays as it is, and the run goes on."""
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(b"# Apples ripen in the late summer sun.\nx = 1\n")
    broken = tmp_path / "broken.py"
    broken.write_bytes(b"# Apples ripen in the late summer sun.\ndef (:\n")
    with caplog.at_level(logging.WARNING):
        report = fix_long_lines([orchard, broken], line_length=LIMIT)
    assert report == {orchard: 1}
    assert orchard.read_text(encoding="UTF-8") == (
        "# Apples ripen in the late\n# summer sun.\nx = 1\n"
    )
    assert broken.read_bytes() == b"# Apples ripen in the late summer sun.\ndef (:\n"
    assert "does not parse" in caplog.text


def test_fix_long_lines_leaves_a_file_with_windows_line_endings(tmp_path, caplog):
    """A rewrite would turn its line endings, so the file is left alone."""
    windows = b"# Apples ripen in the late summer sun.\r\nx = 1\r\n"
    orchard = tmp_path / "orchard.py"
    orchard.write_bytes(windows)
    with caplog.at_level(logging.WARNING):
        assert fix_long_lines([orchard], line_length=LIMIT) == {}
    assert orchard.read_bytes() == windows
    assert "Windows line endings" in caplog.text


def test_fix_long_lines_command_names_each_file(tmp_path, monkeypatch):
    """The command reports each file it rewrote, with its count of lines."""
    (tmp_path / "orchard.py").write_bytes(
        b"# Apples ripen in the late summer sun.\nx = 1\n"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        repomatic, ["fix-long-lines", "--line-length", str(LIMIT), "orchard.py"]
    )
    assert result.exit_code == 0, result.output
    assert "orchard.py: wrapped 1 line(s)" in result.output
