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

"""`claude.md` declares who each section is for, and the declarations hold.

`claude.md` § Section audience tags puts an `<!-- audience: ... -->` comment
under every heading. The tag is the contract three consumers read: `repomatic
init` to decide what to write into a downstream repo, `lint-repo` to report
drift on what it wrote, and the `repomatic-audit` skill to tell a managed
section from one the repo authored itself.

A tag nobody checks is a comment, so the checks below make it a contract.
{func}`test_every_section_is_tagged` and
{func}`test_subsection_never_outreaches_parent` hold the file's own shape;
{func}`test_asset_citations_resolve` and
{func}`test_downstream_asset_cites_no_upstream_section` hold the deployed
agents and skills against it, which is where the tags earn their keep: those
files ship verbatim into repos whose `claude.md` is a different document.

Citation matching follows `test_claude_assets.py` in being deliberately
under-inclusive. A `§` counts as naming a section only when a `claude.md` span
introduces it or a known title follows it, so the `§3`/`§9` rubric references
in `awesome-triage` are skipped rather than guessed at.

```{note}
The first run of {func}`test_downstream_asset_cites_no_upstream_section`
caught `grunt-qa` sending downstream Claude to `CLAUDE.md` § Release checklist
and § Documentation sync. Both carry a `{note}` scoping them to
`kdeldycke/repomatic` itself, and neither is present in any of the six
downstream repos, so the agent was routing work through a checklist that could
not apply and was not there to read.
```
"""

from __future__ import annotations

import re

import pytest

from repomatic.agent_md import (
    AUDIENCES,
    BUNDLED_INSTRUCTIONS,
    DOWNSTREAM_AUDIENCES,
    TAG_SCOPES,
    Section,
    parse_sections,
    render_agent_md,
)
from repomatic.bundle import get_data_content
from repomatic.registry import COMPONENTS_BY_NAME, SKILL_FILENAME, RepoScope
from repomatic.tool_runner import verify_via_write_path
from tests.conftest import skip_unless_tool_runs

UPSTREAM_TITLE_SUFFIX = " (upstream maintainers)"
"""Suffix an `upstream` section carries when a deployed asset may still cite it.

`Documentation sync` and `Release checklist` both already end this way, and both
open with a `{note}` scoping them to `kdeldycke/repomatic`. The suffix is what
makes the citation readable from a repo that never receives the section: the
reader learns it does not apply from the reference itself, without following it.
"""

CLAUDE_MD_SPAN_RE = re.compile(r"`(?:CLAUDE|claude)\.md`[^\n]{0,20}?$")
"""A `claude.md` code span trailing the text just before a `§`.

Matching the *lead-in* rather than the section title is what catches a citation
of a section that has since been renamed: the reference is recognised, then
fails to resolve. Keying only off known titles would silently skip it.
"""

_PREAMBLE, UPSTREAM_SECTIONS = parse_sections(get_data_content(BUNDLED_INSTRUCTIONS))
"""The reference document, read the way `repomatic init` reads it.

Through {func}`~repomatic.agent_md.parse_sections` and the bundled path rather
than a second parser of its own, so a shape these tests accept is by
construction a shape the merge understands.
"""

SECTIONS_BY_TITLE = {section.title: section for section in UPSTREAM_SECTIONS}


def parent_titles() -> dict[str, str]:
    """Map each section title to the title of the heading it sits under.

    Derived from heading depth here rather than carried on
    {class}`~repomatic.agent_md.Section`, because the merge never needs it: it
    re-emits sections in upstream order, so the hierarchy comes along for free.
    """
    parents: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for section in UPSTREAM_SECTIONS:
        while stack and stack[-1][0] >= section.level:
            stack.pop()
        parents[section.title] = stack[-1][1] if stack else ""
        stack.append((section.level, section.title))
    return parents


PARENTS = parent_titles()

section_param = pytest.mark.parametrize(
    "section",
    UPSTREAM_SECTIONS,
    ids=[section.title for section in UPSTREAM_SECTIONS],
)


def deployed_assets() -> list[tuple[str, str, RepoScope]]:
    """Every agent and skill `repomatic init` writes into a downstream repo.

    Returned with the scope gating each one, so a citation can be checked
    against the repositories that actually receive the citing file.
    """
    assets = [
        (
            entry.file_id,
            get_data_content(f"{entry.source}/{SKILL_FILENAME}"),
            entry.scope,
        )
        for entry in COMPONENTS_BY_NAME["skills"].files
    ]
    assets.extend(
        (entry.file_id, get_data_content(entry.source), entry.scope)
        for entry in COMPONENTS_BY_NAME["subagents"].files
    )
    return assets


def cited_titles(body: str) -> set[str]:
    """Section titles an asset sends the reader to with a `§` reference.

    A `§` qualifies when a `claude.md` span introduces it, or when a known
    title follows it (the shape a second reference in the same sentence takes,
    as in "§ Testing guidelines and § Linting and formatting").
    """
    by_length = sorted(SECTIONS_BY_TITLE, key=len, reverse=True)
    cited: set[str] = set()
    for match in re.finditer(r"§\s*", body):
        tail = body[match.end() : match.end() + 90]
        title = next((t for t in by_length if tail.startswith(t)), "")
        if title:
            cited.add(title)
        elif CLAUDE_MD_SPAN_RE.search(body[: match.start()]):
            # Introduced as a claude.md reference but naming nothing that
            # exists: a renamed or deleted section, reported as the tail read.
            cited.add(tail.split("\n")[0].strip())
    return cited


deployed_asset = pytest.mark.parametrize(
    ("asset_id", "body", "scope"),
    deployed_assets(),
    ids=[asset_id for asset_id, _body, _scope in deployed_assets()],
)


@section_param
def test_every_section_is_tagged(section: Section) -> None:
    """Each heading declares an audience, and declares one that exists.

    An untagged section has no reach: a sync cannot place it and a reader
    cannot tell whether it binds them.
    """
    assert section.audience, (
        f"§ {section.title} carries no `<!-- audience: ... -->` tag. "
        f"Every section needs one, see claude.md § Section audience tags."
    )
    assert section.audience in AUDIENCES, (
        f"§ {section.title} declares unknown audience {section.audience!r}, "
        f"expected one of {', '.join(AUDIENCES)}."
    )
    assert section.scope in TAG_SCOPES, (
        f"§ {section.title} declares unknown scope {section.scope!r}, "
        f"expected one of {', '.join(TAG_SCOPES)}."
    )


@section_param
def test_subsection_never_outreaches_parent(section: Section) -> None:
    """A subsection reaches no further than the heading it sits under.

    An `all` section under an `upstream` parent would deploy with no heading
    above it, landing downstream as an orphan under whatever section precedes
    it. The same holds for scope: a repo-wide subsection of a
    `scope: package` parent is unreachable in a repo that skips the parent.
    """
    if not PARENTS[section.title]:
        return
    parent = SECTIONS_BY_TITLE[PARENTS[section.title]]
    reaches = section.audience in DOWNSTREAM_AUDIENCES
    parent_reaches = parent.audience in DOWNSTREAM_AUDIENCES
    assert not (reaches and not parent_reaches), (
        f"§ {section.title} is {section.audience}, but its parent "
        f"§ {parent.title} is {parent.audience}: the subsection would deploy "
        f"without its heading context."
    )
    assert not (section.scope == "all" and parent.scope != "all"), (
        f"§ {section.title} has no scope, but its parent § {parent.title} is "
        f"scoped to {parent.scope}: the subsection is unreachable where the "
        f"parent is skipped."
    )


@deployed_asset
def test_asset_citations_resolve(asset_id: str, body: str, scope: RepoScope) -> None:
    """Every `§` an asset points at names a section that exists.

    Assets deploy verbatim while `claude.md` is edited in place, so a renamed
    or dropped section leaves the citation pointing nowhere, in every repo at
    once and with nothing to notice it.
    """
    unresolved = sorted(t for t in cited_titles(body) if t not in SECTIONS_BY_TITLE)
    assert not unresolved, (
        f"{asset_id} cites claude.md sections that do not exist: "
        f"{'; '.join(unresolved)}"
    )


@section_param
def test_superseded_title_names_no_live_section(section: Section) -> None:
    """A `supersedes:` never claims the title of a section still shipping.

    The merge drops every claimed title wherever it finds one, so a title that
    is both superseded and live would delete the live section from the document
    that is supposed to receive it.
    """
    upstream = {s.title for s in UPSTREAM_SECTIONS}
    collisions = sorted(set(section.supersedes) & upstream)
    assert not collisions, (
        f"§ {section.title} supersedes titles that are still live sections: "
        f"{'; '.join(collisions)}"
    )


def test_merge_is_idempotent_and_keeps_local_sections() -> None:
    """A second merge changes nothing, and untagged sections survive both.

    The unattended `sync-repomatic` job runs `init` on every push, so a merge
    that is not a fixed point opens a pull request forever. The local section
    here is what proves the overlay is an overlay: a document repomatic has
    never seen keeps everything it wrote for itself.
    """
    local = "# Guide\n\n## Something local\n\nRepo-specific prose.\n"
    once = render_agent_md(local)
    twice = render_agent_md(once)
    assert once == twice, "Merging an already-merged document is not a no-op."

    _preamble, sections = parse_sections(once)
    kept = [s.title for s in sections if not s.is_managed]
    assert kept == ["Something local"], f"Local sections not preserved: {kept}"
    assert once.startswith("# Guide"), "The repository's own title was replaced."


@pytest.mark.once
def test_merged_document_is_an_mdformat_fixed_point(tmp_path, monkeypatch) -> None:
    """A merged `claude.md` survives `format-markdown` intact.

    `sync-repomatic` writes `claude.md` and `format-markdown` reformats the same
    file on the same push. When the two disagree on the canonical layout they
    ping-pong: one job rewrites the document, the next reformats it, each
    opening its own pull request, and neither converges. See `claude.md` §
    Common maintenance pitfalls.

    The merge re-emits already-formatted upstream text, so this holds today by
    construction. It is the future separator, banner or generated heading that
    would break it, which is exactly what a fixed-point test is for.

    Verified through the write path rather than `mdformat --check`, whose
    verdict is unreliable for a tool carrying a `post_process` fixup.

    Marked `once`: it resolves mdformat and its plugins through uvx, so one
    runner suffices.
    """
    # mdformat has no --config flag, so `run_tool` stages one in the working
    # directory; chdir keeps that write inside this test's tmp_path rather than
    # the repository root.
    monkeypatch.chdir(tmp_path)
    skip_unless_tool_runs("mdformat")

    target = tmp_path / "claude.md"
    target.write_text(
        render_agent_md("# Guide\n\n## Local prose\n\nRepo-specific.\n"),
        encoding="UTF-8",
    )

    _, drifted = verify_via_write_path("mdformat", extra_args=(str(target),))

    assert not drifted, (
        "mdformat rewrites a freshly merged claude.md, so sync-repomatic and "
        "format-markdown will ping-pong on every push. Align claude.md itself "
        "with mdformat's output rather than reformatting the merged document."
    )


def test_merge_emits_no_upstream_only_section() -> None:
    """No `upstream` section reaches a downstream document.

    The complement of {func}`test_every_section_is_tagged`: tagging a section
    `upstream` has to actually keep it home, or the tag is decorative.
    """
    _preamble, merged = parse_sections(render_agent_md(""))
    upstream_only = sorted(
        s.title
        for s in UPSTREAM_SECTIONS
        if s.audience == "upstream" and s.title in {m.title for m in merged}
    )
    assert not upstream_only, (
        f"Upstream-only sections leaked into a downstream document: "
        f"{'; '.join(upstream_only)}"
    )


def test_package_scope_is_withheld_from_a_virtual_project() -> None:
    """A `scope: package` section skips a project with nothing to publish.

    A uv virtual project locks and tests like any Python repo but never ships a
    release, so the release-lane sections would be instructions it cannot act on.
    """
    _preamble, full = parse_sections(render_agent_md("", is_package=True))
    _preamble, virtual = parse_sections(render_agent_md("", is_package=False))
    withheld = {s.title for s in full} - {s.title for s in virtual}
    expected = {s.title for s in UPSTREAM_SECTIONS if s.scope == "package"}
    assert withheld == expected, (
        f"Package-scoped sections withheld ({sorted(withheld)}) do not match "
        f"those tagged for it ({sorted(expected)})."
    )


@deployed_asset
def test_downstream_asset_cites_no_upstream_section(
    asset_id: str, body: str, scope: RepoScope
) -> None:
    """An upstream section cited downstream says so in its own title.

    The citing file lands in a repo whose `claude.md` never receives an
    `upstream` section, so the reference is unfollowable there. It is still
    worth making when the asset serves both sides and the work is genuinely
    upstream-only, but only if the reader can tell without following it: the
    two such sections are already titled `(upstream maintainers)`, and this
    turns that convention into the condition for citing one at all.

    An upstream section without the suffix is not citable from a deployed
    asset. Either give the section the suffix, or let it reach downstream.
    """
    offenders = sorted(
        f"§ {title}"
        for title in cited_titles(body)
        if title in SECTIONS_BY_TITLE
        and SECTIONS_BY_TITLE[title].audience not in DOWNSTREAM_AUDIENCES
        and not title.endswith(UPSTREAM_TITLE_SUFFIX)
    )
    assert not offenders, (
        f"{asset_id} deploys downstream but cites upstream-only sections that "
        f"do not announce it: {'; '.join(offenders)}. Add "
        f"'{UPSTREAM_TITLE_SUFFIX}' to the heading, or widen its audience."
    )
