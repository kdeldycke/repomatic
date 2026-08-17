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

"""Same-page fragment links, checked against the anchors the build produced.

A literal `](#fragment)` is the one cross-reference nothing resolves. A
`{ref}` or `{doc}` role goes through Sphinx, which reports a missing target
under `nitpicky`; a raw fragment is copied into the HTML untouched, so a slug
that never existed ships as a link that looks fine and lands nowhere. The
build stays green because it was never asked a question.

```{caution}
A Markdown link checker cannot stand in for this, because it has to guess the
slug. Measured against `lychee` 0.24.2 on the heading `## The pages.dev
hostname`: myst-parser builds `the-pages-dev-hostname`, lychee's GitHub-style
slugger wants `the-pagesdev-hostname`, and each reports the other as broken.
That disagreement is why this repository excludes intra-docs fragments from
`lychee` altogether, which left the class with no coverage at all until a
`#the-pagesdev-hostname` link shipped against a `the-pages-dev-hostname`
anchor.
```

The built page is the only authority, so that is what this reads. Fragments
come from the Markdown *source* rather than from the rendered HTML, which is
what keeps the check to what an author actually wrote: a theme's own footnote
backrefs and header permalinks never enter, so there is no denylist to keep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

ANCHOR_ATTRIBUTES = frozenset({"id", "name"})
"""HTML attributes a browser will scroll a fragment to."""

DEFAULT_BUILD_DIR = Path("./docs/_build")
"""Where the Sphinx builders in this project's workflows write the site."""

DEFAULT_DOCS_DIR = Path("./docs")
"""Conventional root of a Sphinx source tree."""

FENCE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})")
"""Opening or closing line of a fenced code block."""

FRAGMENT_LINK_RE = re.compile(r"]\(#(?P<fragment>[^)\s]+)")
"""An authored same-page link, `](#fragment)`.

Anchored on the `](#` sequence, which is what makes it same-page: a link to
another document carries a path before its `#` and is Sphinx's problem, not
this one.
"""

INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?:.|\n)*?(?P=ticks)")
"""An inline code span, of any backtick width."""

MARKDOWN_SUFFIX = ".md"
"""Extension of the sources scanned for authored links."""


@dataclass(frozen=True)
class MissingAnchor:
    """One authored fragment with no anchor to land on."""

    source: Path
    """Markdown file that wrote the link."""

    fragment: str
    """The fragment as authored, without its `#`."""

    page: Path
    """Built page the fragment was looked for in."""

    @property
    def message(self) -> str:
        """The finding as a single reportable line."""
        return f"{self.source}: #{self.fragment} matches no anchor in {self.page}."


@dataclass
class AnchorReport:
    """What one sweep over a docs tree found."""

    missing: list[MissingAnchor] = field(default_factory=list)
    """Every authored fragment that resolves to nothing."""

    unbuilt: list[Path] = field(default_factory=list)
    """Sources with no built page, so with nothing to check against.

    A page left out of every toctree, or a fragment file meant only to be
    included by another, lands here. Reported rather than failed: the build
    is what decides which sources become pages, and it is not this check's
    place to second-guess it.
    """

    checked: int = 0
    """How many authored fragments were resolved against a built page."""


class _AnchorCollector(HTMLParser):
    """Collect every fragment target a built page offers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record the `id` and `name` of every element."""
        for name, value in attrs:
            if name in ANCHOR_ATTRIBUTES and value:
                self.anchors.add(value)


def page_anchors(html: str) -> set[str]:
    """Every fragment a built page can be scrolled to.

    :param html: Full source of one built page.
    :return: The `id` and `name` values it carries.
    """
    collector = _AnchorCollector()
    collector.feed(html)
    return collector.anchors


def strip_code(text: str) -> str:
    """Blank out every code span and fenced block of a Markdown source.

    A fence showing `](#example)` documents a link rather than making one, and
    checking it would fail a page for its own example. Lines are replaced
    rather than deleted so a reported line number still points at the source.

    :param text: Markdown source.
    :return: The same text with code content emptied.
    """
    lines = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append("")
            continue
        lines.append("" if in_fence else line)
    return INLINE_CODE_RE.sub("", "\n".join(lines))


def authored_fragments(text: str) -> list[str]:
    """Every same-page fragment a Markdown source links to.

    :param text: Markdown source.
    :return: Fragments without their `#`, in source order, duplicates kept
        out.
    """
    stripped = strip_code(text)
    found = FRAGMENT_LINK_RE.findall(stripped)
    return list(dict.fromkeys(found))


def built_page(source: Path, docs_dir: Path, build_dir: Path) -> Path | None:
    """Locate the page a Markdown source was rendered into.

    Both Sphinx HTML builders are covered by trying each layout in turn:
    `html` writes `{name}.html`, `dirhtml` writes `{name}/index.html`. Probing
    rather than reading `[tool.repomatic] sphinx.builder` keeps the check
    honest about the tree in front of it, and correct for a caller pointed at
    a directory some other builder wrote.

    :param source: The Markdown file.
    :param docs_dir: Root the source tree is relative to.
    :param build_dir: Root of the rendered site.
    :return: The built page, or `None` when the source produced none.
    """
    relative = source.relative_to(docs_dir)
    # Not `with_suffix`, which would read `repomatic.data.md` as stem
    # `repomatic` plus suffix `.data` and look for `repomatic.html`.
    bare = relative.parent / relative.name.removesuffix(MARKDOWN_SUFFIX)
    for candidate in (build_dir / f"{bare}.html", build_dir / bare / "index.html"):
        if candidate.is_file():
            return candidate
    return None


def markdown_sources(docs_dir: Path, build_dir: Path) -> Iterator[Path]:
    """Every authored Markdown source under a docs tree.

    Skips the rendered site, which commonly sits inside the source tree, and
    every underscore-prefixed directory, Sphinx's own convention for the
    static and template folders that hold no authored prose.

    :param docs_dir: Root of the documentation sources.
    :param build_dir: Root of the rendered site, excluded when nested.
    :return: The sources, in path order.
    """
    resolved_build = build_dir.resolve()
    for path in sorted(docs_dir.rglob(f"*{MARKDOWN_SUFFIX}")):
        if resolved_build in path.resolve().parents:
            continue
        if any(part.startswith("_") for part in path.relative_to(docs_dir).parts[:-1]):
            continue
        yield path


def check_anchors(docs_dir: Path, build_dir: Path) -> AnchorReport:
    """Resolve every authored fragment against the page it was built into.

    :param docs_dir: Root of the documentation sources.
    :param build_dir: Root of the rendered site.
    :return: What the sweep found.
    """
    report = AnchorReport()
    for source in markdown_sources(docs_dir, build_dir):
        fragments = authored_fragments(source.read_text(encoding="UTF-8"))
        if not fragments:
            continue
        page = built_page(source, docs_dir, build_dir)
        if page is None:
            report.unbuilt.append(source)
            continue
        anchors = page_anchors(page.read_text(encoding="UTF-8"))
        for fragment in fragments:
            report.checked += 1
            if fragment not in anchors:
                report.missing.append(MissingAnchor(source, fragment, page))
    return report
