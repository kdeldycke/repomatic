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

"""Splitting a Markdown document into its YAML frontmatter and body.

Two unrelated families of bundled Markdown carry frontmatter: skill definitions
(`SKILL.md`, whose fields the [Agent Skills
spec](https://agentskills.io/specification) defines) and PR body templates in
`repomatic/templates/`. Both need the same split, so it lives here once rather
than once per consumer.
"""

from __future__ import annotations

import yaml

DELIMITER = "---"
"""Line that opens and closes a frontmatter block."""

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


def split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a document into its parsed frontmatter mapping and its body.

    Values keep their YAML types, so a nested field (the spec's `metadata`
    mapping, a template's `args` list) reads back as the structure it was
    written as rather than a flat string.

    ```{note}
    Both delimiters must sit alone on their own line, per the frontmatter
    convention. Scanning for the closing line, instead of splitting the document
    on the first two `---` runs, keeps a value that embeds `---` (like an
    `argument-hint` listing a long-form option) from truncating the block.
    ```

    :param raw: Full text of the document.
    :return: `(frontmatter, body)`. The frontmatter is an empty mapping when the
        document opens no block, leaves one unterminated, or holds something
        other than a YAML mapping; in each of those cases the body is *raw*
        unchanged, so no content is ever silently dropped.
    """
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIMITER:
        return {}, raw
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == DELIMITER:
            break
    else:
        # Unterminated block: treat the whole document as body.
        return {}, raw
    parsed = yaml.safe_load("".join(lines[1:index]))
    if not isinstance(parsed, dict):
        return {}, raw
    return parsed, "".join(lines[index + 1 :]).lstrip("\n")
