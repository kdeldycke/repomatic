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

"""Tests for :mod:`repomatic.markdown` sanitizers."""

from __future__ import annotations

import pytest

from repomatic.markdown import demote_markdown_headings, sanitize_markdown_mentions

ZWS = "\u200b"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The shallowest heading lands on the floor; deeper ones keep their
        # relative depth.
        ("## Harvest\n\n### Apples", "##### Harvest\n\n###### Apples"),
        # An h1-led body shifts further down to reach the same floor.
        ("# Orchard report", "##### Orchard report"),
        # Levels pushed past h6 (markdown's deepest) are clamped.
        ("## Harvest\n\n#### Apples", "##### Harvest\n\n###### Apples"),
        # Headings already at or below the floor are never promoted.
        ("##### Apples\n\n###### Pears", "##### Apples\n\n###### Pears"),
        # Up to 3 leading spaces still form a heading and are preserved.
        ("  ## Cellar", "  ##### Cellar"),
        # Fenced code blocks pass through untouched.
        (
            "## Recipe\n\n```bash\n# a comment\n```",
            "##### Recipe\n\n```bash\n# a comment\n```",
        ),
        # A 4-space indent is a code block; `#word` and `#123` are prose.
        ("    # indented code", "    # indented code"),
        ("#hashtag", "#hashtag"),
        # No headings at all.
        ("plain prose", "plain prose"),
        ("", ""),
    ],
)
def test_demote_markdown_headings(text, expected):
    """Headings are uniformly demoted so the shallowest lands on the floor."""
    assert demote_markdown_headings(text, floor=5) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Thanks @papaya!", f"Thanks @{ZWS}papaya!"),
        ("Ping @orchard/pickers", f"Ping @{ZWS}orchard/pickers"),
        ("Fixes #12 and #3", f"Fixes #{ZWS}12 and #{ZWS}3"),
        (
            "See https://github.com/orchard/mango",
            "See https://redirect.github.com/orchard/mango",
        ),
        # An email address and a URL fragment are not mentions or references.
        ("Mail kiwi@example.com", "Mail kiwi@example.com"),
        ("Read page#12", "Read page#12"),
        # Code is shielded, inline and fenced alike.
        ("Run `@decorator #1`", "Run `@decorator #1`"),
        ("```\n@papaya #7\n```", "```\n@papaya #7\n```"),
        ("", ""),
    ],
)
def test_sanitize_markdown_mentions(text, expected):
    """Mentions and references are broken in prose, and only in prose."""
    assert sanitize_markdown_mentions(text) == expected
