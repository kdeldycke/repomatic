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

"""Generate PR body with workflow metadata for auto-created pull requests.

Callers inject a {class}`~repomatic.metadata.core.Metadata` instance for CI
context to produce a collapsible `<details>` block containing a metadata
table (the injection keeps this module import-cycle-free: `Metadata` pulls
in half the package). Template prefixes are loaded from markdown files in
`repomatic/templates/`, optionally with YAML frontmatter for templates that
require arguments.

```{note}
{func}`load_template` and the `render_*` helpers also accept a
{class}`~pathlib.Path` to read a template from disk. Downstream repos can ship
project-specific templates and feed them through `repomatic pr-body
--template-file path/to/template.md` (paired with one or more
`--template-arg KEY=VALUE` entries to fill the placeholders) without forking
repomatic. External templates should set `footer: false` in their frontmatter
to avoid duplicating the attribution footer that already ships with the
metadata block.
```

Also provides two helpers for embedding externally-sourced markdown in PR or
issue bodies: {func}`sanitize_markdown_mentions` neutralizes `@mentions`,
`#issue` refs, and GitHub URLs, and {func}`demote_markdown_headings` pushes
the embedded content's headings below the embedding document's own sections.
"""

from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from functools import cache
from importlib.resources import as_file, files
from pathlib import Path
from string import Template

from .. import __version__
from ..frontmatter import split_frontmatter
from .actions import extract_workflow_filename, trim_to_budget
from .releases import dev_release_url_and_previous_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..metadata.core import Metadata

GITHUB_BODY_MAX_CHARS = 65536
"""GitHub's maximum PR and issue body size, in UTF-16 code units.

GitHub's API rejects longer bodies outright, so an oversized body has to be
trimmed before it is posted. The obvious trim is the wrong one: cutting the
**end** (what a plain `body[:65536]` does, and what
[peter-evans/create-pull-request](https://github.com/peter-evans/create-pull-request)
did before {mod}`repomatic.github.pr` replaced it) drops whatever sits last,
which here is the refresh tip, the metadata block and the attribution footer:
the navigational parts a reader needs most when a report is too long to read.
{func}`build_pr_body` (PRs) and {func}`fit_github_body` (issues) therefore trim
the leading content instead, so the tail always survives.
"""

_TRUNCATION_NOTICE = "> [!CAUTION]\n> Report truncated to fit GitHub's body size limit."
"""Admonition replacing the content dropped by {func}`build_pr_body` and
{func}`fit_github_body`."""

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


def _stash_fenced_blocks(text: str) -> tuple[str, list[str]]:
    """Replace fenced code blocks with placeholders.

    Fenced blocks are extracted first (they may contain inline backticks) so
    later prose rewrites cannot touch code. Returns the substituted text and the
    extracted blocks, restored later by {func}`_restore_fenced_blocks`.
    """
    fenced_blocks: list[str] = []

    def stash(match: re.Match[str]) -> str:
        fenced_blocks.append(match.group(0))
        return f"\x00FENCED{len(fenced_blocks) - 1}\x00"

    return _FENCED_CODE_BLOCK_RE.sub(stash, text), fenced_blocks


def _restore_fenced_blocks(text: str, blocks: list[str]) -> str:
    """Restore fenced code blocks stashed by {func}`_stash_fenced_blocks`."""
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00FENCED{i}\x00", block)
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
    [dependabot/dependabot-core#1421](https://github.com/dependabot/dependabot-core/issues/1421)) and Renovate
    (ongoing restoration pass edge cases, [renovatebot/renovate#8823](https://github.com/renovatebot/renovate/issues/8823),
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

    # Phase 1: Extract code blocks into placeholders.
    # Fenced blocks first (they may contain inline backticks).
    result, fenced_blocks = _stash_fenced_blocks(text)

    # Inline code spans second.
    inline_spans: list[str] = []

    def _stash_inline(match: re.Match[str]) -> str:
        inline_spans.append(match.group(0))
        return f"\x00INLINE{len(inline_spans) - 1}\x00"

    result = _INLINE_CODE_RE.sub(_stash_inline, result)

    # Phase 2: Sanitize prose content.
    # URLs first, before @ in URLs gets a zero-width space.
    result = _GITHUB_URL_RE.sub(r"\1redirect.github.com/", result)
    result = _MENTION_RE.sub(
        lambda m: f"@{_ZERO_WIDTH_SPACE}{m.group(1)}",
        result,
    )
    result = _ISSUE_REF_RE.sub(
        lambda m: f"#{_ZERO_WIDTH_SPACE}{m.group(1)}",
        result,
    )

    # Phase 3: Restore placeholders (reverse order of extraction).
    for i, span in enumerate(inline_spans):
        result = result.replace(f"\x00INLINE{i}\x00", span)
    result = _restore_fenced_blocks(result, fenced_blocks)

    return result


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

    result, fenced_blocks = _stash_fenced_blocks(text)

    levels = [len(m.group(2)) for m in _ATX_HEADING_RE.finditer(result)]
    shift = floor - min(levels) if levels else 0
    if shift > 0:
        result = _ATX_HEADING_RE.sub(
            lambda m: m.group(1) + "#" * min(6, len(m.group(2)) + shift),
            result,
        )

    return _restore_fenced_blocks(result, fenced_blocks)


def _unescape_dollars(text: str) -> str:
    r"""Replace `\$` with `$` in template text.

    ```{caution}
    Workaround for mdformat escaping `$` characters in markdown files.
    Templates use `string.Template` (`$variable` syntax), but mdformat
    rewrites `$var` as `\$var`. We undo this at load time so that
    `string.Template.substitute()` sees the original placeholders.
    ```
    """
    return text.replace(r"\$", "$")


def _parse_template(raw: str) -> tuple[dict[str, object], str]:
    """Split a template file into YAML frontmatter and markdown body.

    Delegates the split to {func}`~repomatic.frontmatter.split_frontmatter` and
    adds the one template-specific step: restoring the `$placeholder` syntax
    mdformat escapes (see {func}`_unescape_dollars`). The body is unescaped, the
    frontmatter is not: its values are metadata (`args`, `title`, `docs`), never
    `string.Template` sources.

    :param raw: Raw template file content.
    :return: A tuple of (frontmatter dict, body string).
    """
    meta, body = split_frontmatter(raw)
    return meta, _unescape_dollars(body)


@cache
def _load_bundled_template(name: str) -> tuple[dict[str, object], str]:
    """Load and parse a packaged template, once per process.

    Bundled resources are immutable for the life of the interpreter, and one
    PR sync reads the same template up to six times (title, commit message,
    labels, draft state, docs link, body), so the parse is memoized. Callers
    treat the frontmatter mapping as read-only.

    :param name: Template name without extension.
    :return: A tuple of (frontmatter metadata dict, template body string).
    :raises FileNotFoundError: If no such packaged resource exists.
    """
    template_files = files("repomatic.templates")
    for ext in (".md.noformat", ".md"):
        resource = template_files.joinpath(f"{name}{ext}")
        if resource.is_file():
            with as_file(resource) as path:
                raw = path.read_text(encoding="UTF-8")
            return _parse_template(raw)
    msg = f"Template {name!r} not found in repomatic/templates/"
    raise FileNotFoundError(msg)


def load_template(name: str | Path) -> tuple[dict[str, object], str]:
    """Load a PR body template by name or filesystem path.

    Dispatch is type-based:

    - `str` (e.g. `"bump-version"`): looked up as a packaged resource under
      `repomatic.templates`, cached for the process (see
      {func}`_load_bundled_template`). Tries `{name}.md.noformat` first, then
      `{name}.md`. The `.md.noformat` extension is used for templates whose
      `string.Template` placeholders confuse mdformat (e.g. `$rerun_entry`
      prefixed to a list line is parsed as literal text, breaking the list
      structure). See `pr-metadata.md.noformat` for the canonical example.
    - `Path`: read directly from the filesystem, never cached, so a downstream
      repo iterating on a project-specific template sees each edit.

    :param name: Template name without extension, or a {class}`~pathlib.Path`
        pointing to a template file.
    :return: A tuple of (frontmatter metadata dict, template body string).
    :raises FileNotFoundError: If neither resource nor file exists.
    """
    if isinstance(name, Path):
        if not name.is_file():
            msg = f"Template file {str(name)!r} does not exist"
            raise FileNotFoundError(msg)
        return _parse_template(name.read_text(encoding="UTF-8"))
    return _load_bundled_template(name)


def _substitute(text: str, kwargs: dict[str, str | None]) -> str:
    """Apply `string.Template` substitution if kwargs are provided.

    `None` values are normalized to empty strings so callers can pass
    {class}`~repomatic.metadata.core.Metadata` properties directly without
    coercing at the call site.
    """
    if kwargs:
        safe = {k: (v if v is not None else "") for k, v in kwargs.items()}
        return Template(text).substitute(safe)
    return text


def _render_single(name: str | Path, kwargs: dict[str, str | None]) -> tuple[str, bool]:
    """Render a single template and return its body with footer preference.

    A template opts out of the attribution footer with `footer: false`. Both the
    boolean and the quoted string are honored: the frontmatter is YAML, so a bare
    `false` parses as a boolean, but a downstream template may well have quoted
    it, and the two spellings mean the same thing to whoever wrote them.

    :param name: Template name without `.md` extension, or a
        {class}`~pathlib.Path` pointing to a template file.
    :param kwargs: Variables to substitute into the template.
    :return: A tuple of (rendered body, wants_footer).
    """
    meta, body = load_template(name)
    result = _substitute(body, kwargs).strip()
    opted_out = meta.get("footer", True) in (False, "false")
    wants_footer = not opted_out and name != "generated-footer"
    return result, wants_footer


def render_template(*names: str | Path, **kwargs: str | None) -> str:
    """Load and render one or more templates with variable substitution.

    When multiple template names are given, each is rendered and joined with
    a blank line. The `generated-footer` attribution is appended
    once at the end if **any** of the templates wants it (i.e. does not have
    `footer: false` in its frontmatter).

    Static templates (no `$variable` placeholders) are returned as-is.
    Dynamic templates use `string.Template` (`$variable` syntax) to avoid
    conflicts with markdown braces like `[tool.repomatic]`.

    Consecutive blank lines left by empty variables are collapsed to a single
    blank line.

    ```{note}
    The footer's version is `__version__` as read from the working tree, which
    makes it cosmetically wrong on the version-machinery PRs. That is accepted
    rather than fixed. Upstream runs the CLI from its own checkout
    ({data}`~repomatic.release.prepare_release.LOCAL_CLI_INVOCATION`), and both the
    `bump-version` and `prepare-release` jobs rewrite `__version__` with
    `bump-my-version` before this renders, so each of those bodies advertises
    the version its own PR produces (the `minor` or `major` bump target, or
    the post-release patch bump) instead of the identical code that rendered
    it. Both fixes cost more than the tag is worth: rendering the body before
    the bump means splitting `pr-body` back out of `pr-sync`, and feeding the
    pre-bump version in means a CLI option that exists only to work around
    step ordering. Downstream never sees it, since a frozen workflow runs
    `uvx 'repomatic==X.Y.Z'` and takes its version from the installed
    distribution.
    ```

    :param names: One or more template names (without `.md` extension) or
        {class}`~pathlib.Path` objects pointing to template files.
    :param kwargs: Variables to substitute into all templates.
    :return: The rendered markdown string.
    """
    parts = []
    append_footer = False
    for name in names:
        body, wants_footer = _render_single(name, kwargs)
        parts.append(body)
        if wants_footer:
            append_footer = True
    result = "\n\n".join(parts)
    if append_footer:
        result += "\n\n---\n\n" + render_template(
            "generated-footer", version=__version__
        )
    result = re.sub(r"\n{3,}", "\n\n", result)
    if append_footer:
        result += "\n"
    return result


def render_title(name: str | Path, **kwargs: str | None) -> str:
    """Load and render a template's PR title with variable substitution.

    :param name: Template name without `.md` extension, or a
        {class}`~pathlib.Path` pointing to a template file.
    :param kwargs: Variables to substitute into the title.
    :return: The rendered title string, or an empty string when the template
        has no `title` field in its frontmatter.
    """
    meta, _body = load_template(name)
    title = meta.get("title")
    if not title or not isinstance(title, str):
        return ""
    return _substitute(title, kwargs)


def render_commit_message(name: str | Path, **kwargs: str | None) -> str:
    """Load and render a template's commit message with variable substitution.

    Falls back to the `title` if no `commit_message` is defined, and to an
    empty string if neither is set (templates without a title or commit
    message render only their body).

    :param name: Template name without `.md` extension, or a
        {class}`~pathlib.Path` pointing to a template file.
    :param kwargs: Variables to substitute into the commit message.
    :return: The rendered commit message string, or an empty string when the
        template defines neither `commit_message` nor `title`.
    """
    meta, _body = load_template(name)
    commit_msg = meta.get("commit_message")
    if commit_msg and isinstance(commit_msg, str):
        return _substitute(commit_msg, kwargs)
    return render_title(name, **kwargs)


def template_args(name: str | Path) -> list[str]:
    """Return the list of required arguments for a template.

    :param name: Template name without `.md` extension, or a
        {class}`~pathlib.Path` pointing to a template file.
    :return: List of argument names from the frontmatter `args` field.
    """
    meta, _body = load_template(name)
    args = meta.get("args", [])
    if isinstance(args, list):
        return args
    return []


def template_labels(name: str | Path) -> list[str]:
    """Return the labels a template's pull request should carry.

    Read from the `labels:` frontmatter key, accepting a YAML list or a single
    string. The template is the one place that knows which operation it fronts,
    so its labels live beside its title and commit message rather than being
    repeated in every workflow step that opens the PR.

    :param name: Template name without extension, or a template file path.
    :return: The labels, empty when the frontmatter declares none.
    """
    meta, _ = load_template(name)
    labels = meta.get("labels", [])
    if isinstance(labels, str):
        return [labels]
    if isinstance(labels, list):
        return [str(label) for label in labels]
    return []


def template_draft(name: str | Path) -> bool:
    """Return whether a template's pull request should be held in draft.

    Read from the `draft:` frontmatter key. Both the YAML boolean and its
    quoted spelling are honored, mirroring the `footer:` key.

    :param name: Template name without extension, or a template file path.
    """
    meta, _ = load_template(name)
    return meta.get("draft", False) in (True, "true")


def template_docs_url(name: str | Path) -> str:
    """Return a template's documentation deep link, if it declares one.

    PR templates carry a `docs:` frontmatter field pointing at their job's
    section of the hosted workflows reference, surfaced as the
    `Documentation` entry of the metadata block now that PR bodies have no
    description section.

    :param name: Template name without `.md` extension, or a
        {class}`~pathlib.Path` pointing to a template file.
    :return: The URL from the frontmatter `docs` field, or an empty string.
    """
    meta, _body = load_template(name)
    docs = meta.get("docs", "")
    return docs if isinstance(docs, str) else ""


def template_stem(filename: str) -> str:
    """Return a template's name, shorn of its `.md` or `.md.noformat` extension.

    The one place that knows how template filenames decompose: `.md.noformat`
    files are renamed `.md` files hidden from mdformat (see
    {func}`load_template`), so both extensions strip down to the same name.
    Callers derive a PR branch or a documentation label from a
    `--template-file` path with it, and {func}`get_template_names` names the
    bundled templates through it.

    :param filename: A template file's basename.
    :return: The name with neither extension.
    """
    return filename.removesuffix(".noformat").removesuffix(".md")


def get_template_names() -> list[str]:
    """Discover all available template names from the templates package.

    :return: Sorted list of template names (without `.md` extension).
    """
    template_dir = files("repomatic.templates")
    names = []
    for item in template_dir.iterdir():
        item_name = getattr(item, "name", str(item))
        if item_name.endswith((".md", ".md.noformat")):
            names.append(template_stem(item_name))
    return sorted(names)


def generate_pr_metadata_block(
    md: Metadata, docs_url: str = "", docs_name: str = ""
) -> str:
    """Generate a collapsible metadata block from CI context.

    Reads the `GITHUB_*` environment context from *md* and returns a
    markdown `<details>` block listing the workflow metadata fields.

    :param md: The {class}`~repomatic.metadata.core.Metadata` instance to read
        CI context from.
    :param docs_url: Optional deep link to the job's section of the hosted
        workflows reference, rendered as the leading `Documentation` entry.
        Comes from the PR template's `docs:` frontmatter field (see
        {func}`template_docs_url`).
    :param docs_name: Label for the documentation link, usually the template
        (operation) name. Without it the raw URL renders as an autolink.
    :return: A markdown string with the metadata block.
    """
    sha = md.sha or ""
    actor = md.event_actor or ""
    triggering_actor = md.triggering_actor
    rerun_entry = ""
    if triggering_actor and triggering_actor != actor:
        rerun_entry = f"- **Re-run by**: @{triggering_actor}\n"
    docs_entry = ""
    if docs_url:
        link = f"[`{docs_name}`]({docs_url})" if docs_name else f"<{docs_url}>"
        docs_entry = f"- **Documentation**: {link}\n"

    return render_template(
        "pr-metadata",
        docs_entry=docs_entry,
        event_name=md.event_name,
        actor=actor,
        rerun_entry=rerun_entry,
        ref_name=md.ref_name,
        repo_url=md.repo_url,
        sha=sha,
        sha_short=sha[:8],
        job=md.job_name,
        workflow_file=extract_workflow_filename(md.workflow_ref),
        run_id=md.run_id,
        run_number=md.run_number,
        run_attempt=md.run_attempt,
    )


def generate_refresh_tip(md: Metadata) -> str:
    """Generate a tip admonition inviting users to refresh the PR manually.

    Reads the repository URL and `GITHUB_WORKFLOW_REF` from *md* to build
    the workflow dispatch URL.

    :param md: The {class}`~repomatic.metadata.core.Metadata` instance to read
        CI context from.
    :return: A GitHub-flavored markdown `[!TIP]` blockquote, or an empty
        string if the workflow reference is unavailable.
    """
    workflow_file = extract_workflow_filename(md.workflow_ref)
    if not workflow_file:
        return ""
    return render_template(
        "refresh-tip",
        repo_url=md.repo_url,
        workflow_file=workflow_file,
    )


def build_release_review_steps(md: Metadata, version: str) -> tuple[str, str]:
    """Build the two optional review steps of the prepare-release checklist.

    The `How-to release` list opens with two review steps that render only
    when their GitHub data is reachable, so the checklist degrades to the bare
    merge instructions offline (or when the dev pre-release is disabled):

    - **Dev pre-release review**: links the rolling `v{version}.dev0` draft
      through its {attr}`~repomatic.github.releases.ReleaseWithAssets.html_url`
      (drafts have no public tag URL). Omitted when no such draft is visible.
    - **Full-changes review**: links the `v{previous}...main` comparison.
      Omitted when no prior release exists to compare against.

    Both come from a single {func}`dev_release_url_and_previous_version`
    lookup. Each returned string is a complete ordered-list line ending in a
    newline (or empty), written with a `1.` marker so the surrounding lazily
    numbered list renumbers correctly however many steps survive.

    :param md: CI context, read for the repository URL.
    :param version: The release version being prepared (e.g. `1.2.3`).
    :return: A `(dev_release_review, changes_review)` pair of list-item lines.
    """
    repo_url = md.repo_url
    if not repo_url:
        return "", ""
    dev_release_url, previous_version = dev_release_url_and_previous_version(
        repo_url, version
    )

    dev_release_review = ""
    if dev_release_url:
        dev_release_review = (
            f"1. Review the [`v{version}.dev0` GitHub release]({dev_release_url})\n"
        )

    changes_review = ""
    if previous_version:
        changes_review = (
            "1. Review the full changes: "
            f"[`v{previous_version}...main`]"
            f"({repo_url}/compare/v{previous_version}...main)\n"
        )

    return dev_release_review, changes_review


def _utf16_len(text: str) -> int:
    """Length of *text* in UTF-16 code units.

    GitHub's body size limit and create-pull-request's truncation both count
    JavaScript string length (UTF-16 code units), not Unicode code points, so
    emoji-heavy reports must be measured the same way.
    """
    return len(text.encode("utf-16-le")) // 2


def _trim_to_budget(text: str, budget: int) -> str:
    """Keep the leading whole lines of *text* that fit in *budget*.

    {func}`~repomatic.github.actions.trim_to_budget` in the body-content unit,
    UTF-16 code units.

    :param text: Content to trim.
    :param budget: Available room, in UTF-16 code units.
    :return: The kept lines, right-stripped; empty when nothing fits.
    """
    return trim_to_budget(text, budget, _utf16_len)


def build_pr_body(prefix: str, metadata_block: str, refresh_tip: str = "") -> str:
    """Concatenate prefix, refresh tip, and metadata block into a PR body.

    The `metadata_block` already includes the attribution footer (appended
    automatically by {func}`render_template`); the *refresh_tip* comes
    pre-rendered from {func}`generate_refresh_tip` (empty to omit it).

    Bodies over {data}`GITHUB_BODY_MAX_CHARS` have their prefix trimmed to
    fit, replacing the dropped lines with a caution admonition, so the refresh
    tip, metadata block, and attribution footer always survive. Left alone,
    GitHub-side truncation would chop the body from the end instead.

    :param prefix: Content to prepend before the metadata block. Can be empty.
    :param metadata_block: The collapsible metadata block from
        {func}`generate_pr_metadata_block`, with footer.
    :param refresh_tip: Pre-rendered refresh-tip admonition, or empty.
    :return: The complete PR body string.
    """
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if refresh_tip:
        parts.append(refresh_tip)
    parts.append(metadata_block)
    body = "\n\n\n".join(parts)
    if not prefix or _utf16_len(body) <= GITHUB_BODY_MAX_CHARS:
        return body

    # Rebuild with the prefix cut down to whatever budget the fixed tail
    # leaves.
    tail = "\n\n\n".join([_TRUNCATION_NOTICE, *parts[1:]])
    budget = GITHUB_BODY_MAX_CHARS - _utf16_len(tail) - len("\n\n\n")
    truncated_prefix = _trim_to_budget(prefix, budget)
    if not truncated_prefix:
        return tail
    return f"{truncated_prefix}\n\n\n{tail}"


def fit_github_body(body: str) -> str:
    """Trim an oversized issue or pull-request body, keeping the footer.

    The {func}`build_pr_body` counterpart for bodies rendered straight from a
    footer-carrying template (broken-links report, setup guide), and the
    safety net {func}`~repomatic.github.pr.upsert_pr` runs over an explicit
    `--body` that never went through {func}`build_pr_body`. The GitHub API
    rejects oversized bodies outright (`gh issue create`, `gh issue edit` and
    `gh pr create` all fail), so the content above the attribution footer is
    trimmed on line boundaries, with a caution admonition marking the cut.

    :param body: The rendered body, attribution footer included.
    :return: The body unchanged when it fits, else trimmed to fit.
    """
    if _utf16_len(body) <= GITHUB_BODY_MAX_CHARS:
        return body
    footer_suffix = (
        "\n\n---\n\n" + render_template("generated-footer", version=__version__) + "\n"
    )
    if body.endswith(footer_suffix):
        content = body.removesuffix(footer_suffix)
        tail = f"\n\n{_TRUNCATION_NOTICE}{footer_suffix}"
    else:
        # No recognizable footer (external or hand-built body): still trim to
        # fit, without resurrecting a footer that was never there.
        content = body
        tail = f"\n\n{_TRUNCATION_NOTICE}\n"
    trimmed = _trim_to_budget(content, GITHUB_BODY_MAX_CHARS - _utf16_len(tail))
    if not trimmed:
        return tail.lstrip()
    return f"{trimmed}{tail}"


@contextmanager
def temp_body_file(body: str) -> Iterator[Path]:
    """Materialize a rendered body as a temporary file, then remove it.

    The `gh` CLI takes a body only through `--body-file`, so every write path
    against an issue or a pull request needs one on disk. Owning the temp file
    at this layer keeps callers working in the currency they actually produce
    (rendered markdown) instead of each repeating the same write / `try` /
    `unlink` envelope.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        encoding="UTF-8",
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
