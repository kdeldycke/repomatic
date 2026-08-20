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

"""Remove the table-of-contents entries awesome-lint forbids.

Backs the `fix-awesome-toc` command, which runs on `awesome-*` repositories
right after `repomatic run mdformat` regenerates the ToC of every readme.

```{note}
This is the one operation that has no job, PR branch or template of its own,
against the rule in `claude.md` § Naming conventions for automated operations.
It corrects what `format-markdown` just wrote, so it has to share that job's
working tree: given its own job, the two would land in separate PRs and undo
each other on every push, `format-markdown` re-adding the entries this command
had removed.
```

`mdformat-toc` lists every heading in range and offers no exclusion mechanism
of its own, so the entries have to be deleted afterwards.

```{todo}
Delete this module once `mdformat-toc` can express the exclusion in the ToC
marker itself, through
[hukkin/mdformat-toc#17](https://github.com/hukkin/mdformat-toc/issues/17) or
[hukkin/mdformat-toc#20](https://github.com/hukkin/mdformat-toc/pull/20).
```
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

FORBIDDEN_HEADINGS: tuple[str, ...] = (
    "Contents",
    "Contributing",
    "Footnotes",
    "Related Lists",
)
"""Headings awesome-lint refuses to see listed in the table of contents.

Mirrors the roster in
[awesome-lint's `toc.js`](https://github.com/sindresorhus/awesome-lint/blob/v2.2.2/rules/toc.js#L14-L18),
plus the heading owning the ToC itself (`Contents`), whose entry trips
`remark-lint:awesome-toc`:

```
✖  26:1  ToC item "Contents" does not match corresponding heading "Meta"
```

These are the English names, the only ones awesome-lint knows. A translated
readme names the same sections in its own language, which is why matching on
this roster alone is not enough: see {func}`forbidden_headings_for`.
"""

README_RE = re.compile(r"^readme(\.[^.]+)?\.md$")
"""Match `readme.md` and every `readme.{lang}.md` translation beside it.

Only the repository root is scanned. The `find ./` this replaced walked the
whole tree, which on a checkout carrying a `node_modules/` directory would
have reached a few hundred vendored readmes.
"""

REFERENCE_README = "readme.md"
"""The English readme, whose heading positions every translation is mapped onto."""

HEADING_RE = re.compile(r"^\#{1,6}[ \t]+(?P<text>.+?)[ \t]*\#*[ \t]*$", re.MULTILINE)
"""Match an ATX heading and capture its text, closing sequence excluded."""

TOC_ENTRY_RE = re.compile(r"^[ \t]*- \[(?P<text>.+)\]\(#[^)]*\)$")
"""Match one `mdformat-toc` list entry and capture its link text."""

TOC_START_RE = re.compile(r"^<!--\s*mdformat-toc\s+start\b.*-->$", re.IGNORECASE)
"""Match the opening marker of an `mdformat-toc` block."""

TOC_END_RE = re.compile(r"^<!--\s*mdformat-toc\s+end\s*-->$", re.IGNORECASE)
"""Match the closing marker of an `mdformat-toc` block."""

FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})")
"""Match the delimiter of a fenced code block."""


def _strip_fenced_blocks(content: str) -> str:
    """Blank out fenced code blocks so their `#` lines are not read as headings.

    Replaces the content of every fence with empty lines, keeping the line
    count intact so positions elsewhere are unaffected.

    :param content: The full Markdown document.
    :return: The document with fenced blocks blanked out.
    """
    lines = content.splitlines()
    kept: list[str] = []
    fence: str | None = None
    for line in lines:
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group("fence")[0]
                kept.append("")
                continue
        elif match and match.group("fence")[0] == fence:
            fence = None
            kept.append("")
            continue
        kept.append("" if fence else line)
    return "\n".join(kept)


def headings(content: str) -> list[str]:
    """List the ATX heading texts of a Markdown document, in document order.

    :param content: The full Markdown document.
    :return: Every heading text, code fences excluded.
    """
    return [m.group("text") for m in HEADING_RE.finditer(_strip_fenced_blocks(content))]


def forbidden_headings_for(
    content: str,
    reference_headings: Sequence[str] | None = None,
) -> set[str]:
    """Resolve which heading texts must not appear in *content*'s ToC.

    Always includes the English {data}`FORBIDDEN_HEADINGS` that awesome-lint
    knows, since a translation routinely leaves some of them untranslated.

    On top of that, when *reference_headings* is given and the document has the
    same number of headings, the heading occupying each forbidden position in
    the reference is forbidden here too. That positional mapping is what carries
    the rule across languages: repomatic cannot know that `贡献` translates
    `Contributing`, but it can see that both sit at the same index of a readme
    and its translation. Headings survive formatting untouched, so the mapping
    holds even against an already-stripped reference.

    :param content: The Markdown document to resolve the roster for.
    :param reference_headings: Headings of {data}`REFERENCE_README`, if any.
    :return: The heading texts whose ToC entry must be deleted.
    """
    forbidden = set(FORBIDDEN_HEADINGS)
    if not reference_headings:
        return forbidden

    own_headings = headings(content)
    if len(own_headings) != len(reference_headings):
        logging.warning(
            f"Heading count differs from {REFERENCE_README} "
            f"({len(own_headings)} vs {len(reference_headings)}): "
            "falling back to English heading names only."
        )
        return forbidden

    forbidden.update(
        own_headings[index]
        for index, heading in enumerate(reference_headings)
        if heading in FORBIDDEN_HEADINGS
    )
    return forbidden


def _toc_bounds(lines: Sequence[str]) -> tuple[int, int] | None:
    """Locate the `mdformat-toc` block in *lines*.

    :param lines: The document split into lines.
    :return: The start and end marker indices, or `None` when either is absent.
    """
    start = end = None
    for index, line in enumerate(lines):
        if start is None:
            if TOC_START_RE.match(line):
                start = index
        elif TOC_END_RE.match(line):
            end = index
            break
    if start is None or end is None:
        return None
    return start, end


def strip_toc_entries(content: str, forbidden: set[str]) -> tuple[str, list[str]]:
    """Delete the ToC entries of *content* whose link text is *forbidden*.

    Only the `mdformat-toc` block is touched: a list item elsewhere in the
    document that happens to link the same heading is left alone.

    :param content: The full Markdown document.
    :param forbidden: Heading texts whose entry must go.
    :return: The updated document and the link texts that were deleted.
    """
    lines = content.splitlines(keepends=True)
    bounds = _toc_bounds([line.rstrip("\r\n") for line in lines])
    if bounds is None:
        return content, []
    start, end = bounds

    kept: list[str] = []
    removed: list[str] = []
    for index, line in enumerate(lines):
        if start < index < end:
            match = TOC_ENTRY_RE.match(line.rstrip("\r\n"))
            if match and match.group("text") in forbidden:
                removed.append(match.group("text"))
                continue
        kept.append(line)
    return "".join(kept), removed


def fix_awesome_toc(root: Path | None = None) -> dict[Path, list[str]]:
    """Strip the forbidden ToC entries from every readme under *root*.

    Reads {data}`REFERENCE_README` first so its heading positions can be mapped
    onto each translation, then rewrites every readme that changed. Idempotent:
    a second run finds nothing left to delete.

    :param root: Directory holding the readmes. Defaults to the current one.
    :return: The deleted entries, keyed by the readme they came from.
    """
    root = Path.cwd() if root is None else root

    reference = root / REFERENCE_README
    reference_headings: list[str] | None = None
    if reference.is_file():
        reference_headings = headings(reference.read_text(encoding="UTF-8"))
    else:
        logging.warning(
            f"No {REFERENCE_README} in {root}: translations can only be matched "
            "on English heading names."
        )

    report: dict[Path, list[str]] = {}
    for readme in _readmes(root):
        content = readme.read_text(encoding="UTF-8")
        forbidden = forbidden_headings_for(content, reference_headings)
        updated, removed = strip_toc_entries(content, forbidden)
        if not removed:
            logging.debug(f"No forbidden ToC entry in {readme}.")
            continue
        readme.write_text(updated, encoding="UTF-8")
        logging.info(f"Removed {len(removed)} ToC entries from {readme}.")
        report[readme] = removed
    return report


def _readmes(root: Path) -> Iterator[Path]:
    """Yield the readmes of *root*, the reference first.

    The reference goes first so its own entries are gone before a translation
    is compared against it, which keeps a partial failure from leaving the
    English readme dirtier than its translations.

    :param root: Directory to scan.
    :return: Each matching readme path, sorted after the reference.
    """
    matches: Iterable[Path] = sorted(
        path for path in root.iterdir() if path.is_file() and README_RE.match(path.name)
    )
    for path in matches:
        if path.name == REFERENCE_README:
            yield path
    for path in matches:
        if path.name != REFERENCE_README:
            yield path
