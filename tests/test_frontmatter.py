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

"""Tests for the shared YAML frontmatter splitter."""

from __future__ import annotations

from textwrap import dedent

import pytest

from repomatic.frontmatter import split_frontmatter


def test_splits_frontmatter_and_body():
    """A well-formed block yields its mapping and the body below it."""
    raw = dedent("""\
        ---
        name: papaya-report
        description: Summarize the papaya harvest.
        ---
        Body text.
        """)
    meta, body = split_frontmatter(raw)
    assert meta == {
        "name": "papaya-report",
        "description": "Summarize the papaya harvest.",
    }
    assert body == "Body text.\n"


def test_values_keep_yaml_types():
    """Nested mappings and lists read back as structures, not strings."""
    raw = dedent("""\
        ---
        args: [city, temperature]
        metadata:
          season: autumn
          crates: 12
        ---
        """)
    meta, _body = split_frontmatter(raw)
    assert meta["args"] == ["city", "temperature"]
    assert meta["metadata"] == {"season": "autumn", "crates": 12}


def test_value_containing_delimiter_survives():
    """A `---` inside a value does not truncate the block.

    The naive "split on the first two `---` runs" approach cuts the block short
    here, dropping every field after `argument-hint` and leaking the remainder
    into the body.
    """
    raw = dedent("""\
        ---
        name: papaya-report
        argument-hint: "[--crates N] [---] [city]"
        description: Summarize the papaya harvest.
        ---
        Body text.
        """)
    meta, body = split_frontmatter(raw)
    assert meta["argument-hint"] == "[--crates N] [---] [city]"
    assert meta["description"] == "Summarize the papaya harvest."
    assert body == "Body text.\n"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("Just a body.\n", id="no-block"),
        pytest.param("---\nname: papaya\nBody with no closing line.\n", id="unclosed"),
        pytest.param("---\n- mango\n- papaya\n---\nBody.\n", id="sequence-not-mapping"),
        pytest.param("---\nplain scalar\n---\nBody.\n", id="scalar-not-mapping"),
    ],
)
def test_no_usable_frontmatter_returns_document_untouched(raw):
    """Every degenerate shape yields an empty mapping and the input as body.

    Returning *raw* rather than a stripped remainder is what keeps a malformed
    block from silently eating content.
    """
    assert split_frontmatter(raw) == ({}, raw)


def test_body_leading_blank_lines_are_trimmed():
    """Blank lines between the closing delimiter and the body are dropped."""
    raw = "---\nname: papaya\n---\n\n\nBody text.\n"
    _meta, body = split_frontmatter(raw)
    assert body == "Body text.\n"
