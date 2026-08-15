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

"""Project the audience-tagged parts of `claude.md` into a downstream repo.

`claude.md` § Section audience tags puts an `<!-- audience: ... -->` comment
under every heading upstream. This module reads those tags and writes the
sections a given repository is entitled to into that repository's own
instructions file, leaving everything it authored for itself untouched.

Upstream's copy is `claude.md` because Claude Code is what reads it here, but
the destination is whatever `[tool.repomatic] agent.location` resolves to,
defaulting through `[tool.repomatic.flavor] agent` to that agent's own filename.
`AGENTS.md` is the same document under the cross-agent convention, and a
repository keeping it outside the root is the case the key exists for.

The merge is the *overlay* half of the pair `init_project` already runs against
`pyproject.toml`: there the bundled template is the base and local keys graft on
top; here the repository's document is the base and the tagged sections overlay
into it. A section is identified by its heading title, which is also its anchor,
so a cross-reference written upstream keeps resolving downstream.

Three rules decide what a repository ends up with:

- **A tagged section is upstream's.** It is re-emitted from the bundled document
  on every sync, so a downstream edit to one is reverted. That is the point: the
  six repositories consuming this today have drifted on roughly four in five of
  the sections they nominally share, silently and in both directions.
- **An untagged section is the repository's.** It is carried through verbatim and
  no sync ever rewrites it, which is where repo-specific knowledge belongs.
- **A title collision resolves upstream's way.** An untagged local section whose
  title matches a tagged one is a hand-copied ancestor of it, and adopting it is
  the whole reason this exists. A repository wanting a section of its own on a
  neighbouring subject gives it a different title.

```{caution}
Ordering is not preserved across the boundary: tagged sections are emitted first
in upstream order, then the repository's own in theirs. A stable order is what
keeps the sync from fighting `format-markdown` for the canonical layout, per
`claude.md` § Common maintenance pitfalls, and it makes the managed block one
contiguous region a reader can skip. The first sync of an existing document
therefore moves its untagged sections down, once.
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .bundle import get_data_content
from .registry import RepoScope

BUNDLED_INSTRUCTIONS = "claude.md"
"""The reference document, bundled under `repomatic/data/` as a symlink.

Kept a symlink back to the repository root rather than a copy, the way the
subagent definitions are, so the file this module ships is the one the
conformance tests in `tests/test_agent_md.py` check.

The name is upstream's own, not the destination's: what a consumer ends up
writing is `[tool.repomatic] agent.location`, which every function here takes as
a parameter rather than reading from this constant.
"""

AUDIENCES = ("all", "upstream", "downstream")
"""Every audience a section may declare.

`all` is upstream plus every consumer, `upstream` never leaves
`kdeldycke/repomatic`, and `downstream` is what a repository needs *because* it
consumes repomatic, which by definition does not describe repomatic itself.
"""

DOWNSTREAM_AUDIENCES = frozenset({"all", "downstream"})
"""Audiences a repository consuming repomatic receives.

`upstream` is the complement and never leaves `kdeldycke/repomatic`, which is
why {func}`merge_agent_md` is skipped there outright rather than filtered: the
source repository would otherwise receive the `downstream` sections written for
its consumers.
"""

TAG_SCOPES = {
    "all": RepoScope.ALL,
    "package": RepoScope.PACKAGE_ONLY,
}
"""Scope qualifiers a tag may carry, mapped onto the registry's own vocabulary.

Deliberately a subset of {class}`~repomatic.registry.RepoScope`: a qualifier is
added when a section demonstrably does not apply somewhere, not in anticipation.
"""

FENCE_RE = re.compile(r"^\s*```")

HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.*)$")

TAG_RE = re.compile(
    r"^<!--\s*audience:\s*(?P<audience>[a-z]+)"
    r"(?:\s*;\s*scope:\s*(?P<scope>[a-z]+))?\s*-->$"
)

SUPERSEDES_RE = re.compile(r"^<!--\s*supersedes:\s*(?P<title>.+?)\s*-->$")
"""A heading title this section replaces, one comment per title.

Renaming a managed section otherwise strands the old one downstream: the merge
keys on the title, so the repository keeps its now-stale copy sitting beside the
corrected replacement, each contradicting the other. This is the same migration
`sync-labels` runs for a renamed label, and `claude.md` § Retiring a label is a
migration, not a deletion is the argument for why a rename beats a drop-and-add.

On its own line rather than folded into {data}`TAG_RE`, because a heading title
may contain the `;` and `:` that would otherwise delimit it.
"""


@dataclass(frozen=True)
class Section:
    """One heading of an instructions file, with its tag and body as written."""

    level: int
    """Heading depth, from 2 (the document title is not a section)."""

    title: str
    """Heading text, which is also the anchor a cross-reference targets."""

    audience: str
    """Declared audience, or the empty string when the section carries no tag."""

    scope: str
    """Declared scope qualifier, defaulting to `all` when the tag omits one."""

    text: str
    """Verbatim source, from the heading line to the line before the next one.

    Kept whole rather than split into heading and body so a re-emitted section
    is byte-identical to its upstream form, down to the trailing blank lines
    `format-markdown` settled on.
    """

    supersedes: tuple[str, ...] = ()
    """Heading titles this section replaces downstream, from {data}`SUPERSEDES_RE`."""

    @property
    def is_managed(self) -> bool:
        """Whether upstream owns this section, rather than the repository."""
        return bool(self.audience)

    def reaches(self, is_awesome: bool, is_python: bool, is_package: bool) -> bool:
        """Whether a repository with these traits receives this section.

        :param is_awesome: `True` for `awesome-*` repositories.
        :param is_python: `True` when a PEP 621 `[project].name` is present.
        :param is_package: `True` when the project also builds a distributable.
        :return: Whether the section is both downstream-bound and in scope.
        :raises KeyError: If the tag carries a scope this module does not know.
            Upstream tags are held to {data}`TAG_SCOPES` by
            `tests/test_agent_md.py`, so this signals a bundled document from a
            newer repomatic than the code reading it.
        """
        if self.audience not in DOWNSTREAM_AUDIENCES:
            return False
        return TAG_SCOPES[self.scope].matches(is_awesome, is_python, is_package)


def parse_sections(content: str) -> tuple[str, list[Section]]:
    """Split an instructions file into its preamble and its sections.

    The preamble is everything above the first section heading: the document
    title, and whatever one-paragraph description a repository put under it.
    Both belong to the repository and survive every sync.

    :param content: Full text of an instructions file.
    :return: The preamble, and one {class}`Section` per heading below level 1.
    """
    lines = content.splitlines()
    # Heading positions first, so a section's text is a plain slice afterwards.
    heads: list[tuple[int, int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := HEADING_RE.match(line)):
            level = len(match.group("level"))
            if level > 1:
                heads.append((index, level, match.group("title").strip()))

    if not heads:
        return content, []

    sections = []
    for position, (start, level, title) in enumerate(heads):
        end = heads[position + 1][0] if position + 1 < len(heads) else len(lines)
        block = lines[start:end]
        audience, scope = "", "all"
        supersedes: list[str] = []
        # The tag block sits right under the heading, so a short lookahead
        # reaches it without ever running into the body. It is one comment per
        # line, so the window has to clear the audience tag plus however many
        # `supersedes:` lines follow it.
        for candidate in block[1:8]:
            stripped = candidate.strip()
            if tag := TAG_RE.match(stripped):
                audience = tag.group("audience")
                scope = tag.group("scope") or "all"
            elif renamed := SUPERSEDES_RE.match(stripped):
                supersedes.append(renamed.group("title"))
            elif stripped and not stripped.startswith("<!--"):
                # First line of prose: the tag block is over.
                break
        sections.append(
            Section(
                level,
                title,
                audience,
                scope,
                "\n".join(block).strip("\n"),
                tuple(supersedes),
            )
        )

    return "\n".join(lines[: heads[0][0]]).strip("\n"), sections


def render_agent_md(
    existing: str,
    *,
    is_awesome: bool = False,
    is_python: bool = True,
    is_package: bool = True,
) -> str:
    """Overlay the sections a repository is entitled to onto its own document.

    Idempotent: rendering an already-merged document returns it unchanged, which
    is what lets `repomatic init` report it as untouched and keeps the unattended
    `sync-repomatic` job from opening a pull request every run.

    :param existing: Current instructions file of the target repository, empty
        when it has none yet.
    :param is_awesome: `True` for `awesome-*` repositories.
    :param is_python: `True` when a PEP 621 `[project].name` is present.
    :param is_package: `True` when the project also builds a distributable.
    :return: The merged document, always newline-terminated.
    """
    upstream_preamble, upstream_sections = parse_sections(
        get_data_content(BUNDLED_INSTRUCTIONS)
    )
    managed = [
        section
        for section in upstream_sections
        if section.reaches(is_awesome, is_python, is_package)
    ]

    local_preamble, local_sections = parse_sections(existing)
    claimed = {section.title for section in managed}
    claimed.update(title for section in managed for title in section.supersedes)
    # A tagged local section is dropped whatever its title: either it is being
    # re-emitted from upstream just below, or upstream stopped sending it here
    # and the copy on disk is an orphan no stale-file check would ever see.
    kept = [
        section
        for section in local_sections
        if not section.is_managed and section.title not in claimed
    ]

    blocks = [local_preamble or upstream_preamble]
    blocks.extend(section.text for section in managed)
    blocks.extend(section.text for section in kept)
    return "\n\n".join(block for block in blocks if block) + "\n"


def merge_agent_md(
    target: Path,
    *,
    is_awesome: bool = False,
    is_python: bool = True,
    is_package: bool = True,
) -> bool:
    """Write the entitled sections into *target*, creating the file if absent.

    :param target: Path to the repository's instructions file, from
        `[tool.repomatic] agent.location`.
    :param is_awesome: `True` for `awesome-*` repositories.
    :param is_python: `True` when a PEP 621 `[project].name` is present.
    :param is_package: `True` when the project also builds a distributable.
    :return: Whether the file was created or modified.
    """
    existing = target.read_text(encoding="UTF-8") if target.is_file() else ""
    merged = render_agent_md(
        existing,
        is_awesome=is_awesome,
        is_python=is_python,
        is_package=is_package,
    )
    if merged == existing:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(merged, encoding="UTF-8")
    return True
