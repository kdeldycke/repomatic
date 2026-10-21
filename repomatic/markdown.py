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

"""Prepare externally-sourced markdown for a pull request or issue body.

{func}`sanitize_markdown_mentions` neutralizes `@mentions`, `#issue` refs and
GitHub URLs, and {func}`demote_markdown_headings` pushes the embedded content's
headings below the embedding document's own sections. Both shield fenced code
blocks, so a `# comment` or an `@decorator` in a sample survives.

A leaf module with no project-internal imports, so any module can import it
without the risk of an import cycle.
"""

from __future__ import annotations

import re

_ZERO_WIDTH_SPACE = "\u200b"
"""Unicode zero-width space inserted to break GitHub's mention/issue parser.

Both Dependabot ([dependabot/dependabot-core#3157](https://github.com/dependabot/dependabot-core/pull/3157),
[link_and_mention_sanitizer.rb](https://github.com/dependabot/dependabot-core/blob/main/common/lib/dependabot/pull_request_creator/message_builder/link_and_mention_sanitizer.rb))
and Renovate ([renovatebot/renovate#1083](https://github.com/renovatebot/renovate/pull/1083),
[markdown.ts](https://github.com/renovatebot/renovate/blob/main/lib/util/markdown.ts))
independently converged on this character to neutralize `@mentions` and
`#issue` references in PR bodies without affecting visual rendering.
"""

_ATX_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})(?=\s|$)", re.MULTILINE)
"""Matches ATX headings per CommonMark: up to 3 leading spaces, then 1-6 `#`
followed by whitespace or end of line. Four-space indents are code blocks and
`#word` without a space is prose, so neither matches."""

_FENCED_CODE_BLOCK_RE = re.compile(
    r"^(`{3,}|~{3,})[^\n]*\n.*?\n\1\s*$",
    re.MULTILINE | re.DOTALL,
)
"""Matches fenced code blocks (backtick or tilde) per CommonMark spec."""

_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
"""Matches inline code spans with one or more backticks."""

_MENTION_RE = re.compile(
    r"(?<![a-zA-Z0-9._%+\-/])@"
    r"([a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?(?:/[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)?)",
)
"""Matches `@username` and `@org/team` mentions in prose.

The negative lookbehind excludes email addresses (`user@example.com`),
URL user-info (`git@github.com`), and URL paths (`/compare/@v1`).
"""

_ISSUE_REF_RE = re.compile(r"(?<![&a-zA-Z0-9/])#(\d+)")
"""Matches `#123` issue/PR references in prose.

The negative lookbehind excludes HTML entities (`&#8203;`), URL fragments
(`page#section`), and path-like references (`path/#1`). Markdown headings
(`# Title`) are excluded because `#` is followed by a space, not a digit.
"""

_GITHUB_URL_RE = re.compile(r"(https?://)(github\.com/)")
"""Matches `github.com` URLs for rewriting to `redirect.github.com`."""


def _stash(pattern: re.Pattern[str], text: str, tag: str) -> tuple[str, list[str]]:
    """Replace each match of *pattern* with a numbered placeholder.

    Code is extracted before prose is rewritten, so no rewrite can touch it.

    :param pattern: What to shield.
    :param text: The text to scan.
    :param tag: Placeholder tag, unique per pattern.
    :return: The substituted text and the extracted matches, restored later by
        {func}`_restore`.
    """
    stashed: list[str] = []

    def stash(match: re.Match[str]) -> str:
        stashed.append(match.group(0))
        return f"\x00{tag}{len(stashed) - 1}\x00"

    return pattern.sub(stash, text), stashed


def _restore(text: str, stashed: list[str], tag: str) -> str:
    """Restore the matches {func}`_stash` replaced under *tag*."""
    for index, block in enumerate(stashed):
        text = text.replace(f"\x00{tag}{index}\x00", block)
    return text


def sanitize_markdown_mentions(text: str) -> str:
    """Neutralize `@mentions`, `#issue` refs, and GitHub URLs in markdown.

    Prevents GitHub from auto-linking mentions and issue references in
    externally-sourced markdown (upstream release notes, third-party tool
    output) that would cause notification spam or accidental issue closure.

    Uses a placeholder extraction approach: fenced code blocks and inline code
    spans are temporarily replaced with unique placeholders before sanitization,
    then restored afterward. This avoids the fragile "sanitize then restore"
    pattern that caused bugs in both Dependabot (2019 code-fence regression,
    [dependabot/dependabot-core#1421](https://github.com/dependabot/dependabot-core/issues/1421))
    and Renovate (ongoing restoration pass edge cases,
    [renovatebot/renovate#8823](https://github.com/renovatebot/renovate/issues/8823),
    [renovatebot/renovate#2554](https://github.com/renovatebot/renovate/issues/2554)).

    Inserts a Unicode zero-width space (U+200B) after `@` and `#` to break
    GitHub's mention and issue parsers without affecting visual rendering.
    Rewrites `github.com` URLs to `redirect.github.com` to prevent backlink
    cross-references on upstream issues.

    :param text: Raw markdown text from an external source.
    :return: Sanitized markdown safe for embedding in a GitHub PR or issue body.

    ```{note}
    Only call this on externally-sourced content (upstream release notes,
    third-party tool output). Do not call on content authored by the
    repository owner where mentions are intentional.
    ```
    """
    if not text:
        return text

    # Fenced blocks first: they may contain inline backticks.
    result, fenced_blocks = _stash(_FENCED_CODE_BLOCK_RE, text, "FENCED")
    result, inline_spans = _stash(_INLINE_CODE_RE, result, "INLINE")

    # URLs first, before @ in URLs gets a zero-width space.
    result = _GITHUB_URL_RE.sub(r"\1redirect.github.com/", result)
    result = _MENTION_RE.sub(lambda m: f"@{_ZERO_WIDTH_SPACE}{m.group(1)}", result)
    result = _ISSUE_REF_RE.sub(lambda m: f"#{_ZERO_WIDTH_SPACE}{m.group(1)}", result)

    # Restore in the reverse order of extraction.
    result = _restore(result, inline_spans, "INLINE")
    return _restore(result, fenced_blocks, "FENCED")


def demote_markdown_headings(text: str, floor: int) -> str:
    """Demote ATX headings so the shallowest one lands at level *floor*.

    Externally-sourced markdown (upstream release notes) carries its own `#`
    and `##` headings, which GitHub renders at full size even inside a
    `<details>` block, so they compete with the embedding document's section
    hierarchy. All headings are shifted deeper by the uniform offset that puts
    the shallowest at *floor*, preserving the body's internal structure;
    levels past `######` (h6, markdown's deepest) are clamped. Headings
    already at or below *floor* are left alone: this function never promotes.

    Fenced code blocks are shielded with the same placeholder extraction as
    {func}`sanitize_markdown_mentions`, so `# comments` in shell samples
    survive. Only ATX headings are rewritten: setext headings (underlined
    with `===` or `---`), rare in release notes, pass through unchanged.

    :param text: Raw markdown text from an external source.
    :param floor: Target level (1-6) for the shallowest heading.
    :return: Markdown with headings demoted.
    """
    if not text:
        return text

    result, fenced_blocks = _stash(_FENCED_CODE_BLOCK_RE, text, "FENCED")

    levels = [len(m.group(2)) for m in _ATX_HEADING_RE.finditer(result)]
    shift = floor - min(levels) if levels else 0
    if shift > 0:
        result = _ATX_HEADING_RE.sub(
            lambda m: m.group(1) + "#" * min(6, len(m.group(2)) + shift),
            result,
        )

    return _restore(result, fenced_blocks, "FENCED")
