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

"""Authored fragment links, resolved against the pages a build produced.

The trees below are hand-written rather than produced by Sphinx: the check
reads a directory of Markdown and a directory of HTML, so a fixture that
spells both out states the case under test in one screen and runs without a
toolchain.
"""

from __future__ import annotations

import pytest

from repomatic.site_anchors import (
    authored_fragments,
    built_page,
    check_anchors,
    markdown_sources,
    page_anchors,
    strip_code,
)


def build_tree(root, sources, pages):
    """Lay out a docs tree and its rendered site.

    :param root: Directory to build both trees under.
    :param sources: `{relative path: Markdown}` for the source tree.
    :param pages: `{relative path: HTML}` for the rendered site.
    :return: The `(docs_dir, build_dir)` pair.
    """
    docs_dir = root / "docs"
    build_dir = docs_dir / "_build"
    for relative, text in sources.items():
        target = docs_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="UTF-8")
    for relative, text in pages.items():
        target = build_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="UTF-8")
    return docs_dir, build_dir


def test_resolving_link_passes(tmp_path):
    """A fragment naming an anchor the page carries is what success looks like."""
    docs_dir, build_dir = build_tree(
        tmp_path,
        {"harvest.md": "See [below](#papaya-yields).\n\n## Papaya yields\n"},
        {"harvest.html": '<h2 id="papaya-yields">Papaya yields</h2>'},
    )
    report = check_anchors(docs_dir, build_dir)
    assert report.missing == []
    assert report.checked == 1


def test_missing_anchor_names_file_fragment_and_page(tmp_path):
    """The failure has to say all three, or it cannot be acted on."""
    docs_dir, build_dir = build_tree(
        tmp_path,
        {"harvest.md": "See [below](#apricot-yields).\n"},
        {"harvest.html": '<h2 id="papaya-yields">Papaya yields</h2>'},
    )
    report = check_anchors(docs_dir, build_dir)
    assert len(report.missing) == 1
    finding = report.missing[0]
    assert finding.source == docs_dir / "harvest.md"
    assert finding.fragment == "apricot-yields"
    assert finding.page == build_dir / "harvest.html"
    assert "harvest.md" in finding.message
    assert "#apricot-yields" in finding.message
    assert "harvest.html" in finding.message


def test_the_slugger_disagreement_this_check_exists_for(tmp_path):
    """The real archetype: a dotted heading, slugged two different ways.

    `## The pages.dev hostname` becomes `the-pages-dev-hostname` under
    myst-parser and `the-pagesdev-hostname` under a GitHub-style slugger.
    A link written to the second spelling survives a green Sphinx build and
    is waved through by a Markdown link checker computing the same wrong
    slug; only the built page settles it, which is what this reads.
    """
    docs_dir, build_dir = build_tree(
        tmp_path,
        {
            "cloudflare.md": (
                "Each project keeps its hostname for life; see"
                " [below](#the-pagesdev-hostname) for why.\n\n"
                "## The pages.dev hostname\n"
            )
        },
        {
            "cloudflare.html": (
                '<h2 id="the-pages-dev-hostname">The pages.dev hostname</h2>'
            )
        },
    )
    report = check_anchors(docs_dir, build_dir)
    assert [finding.fragment for finding in report.missing] == ["the-pagesdev-hostname"]


@pytest.mark.parametrize(
    "page",
    [
        pytest.param("harvest.html", id="html-builder"),
        pytest.param("harvest/index.html", id="dirhtml-builder"),
    ],
)
def test_both_builders_are_resolved(tmp_path, page: str):
    """`html` writes `{name}.html`, `dirhtml` writes `{name}/index.html`.

    Probing for each in turn is what lets one check serve a project on either
    `[tool.repomatic] sphinx.builder` without being told which.
    """
    docs_dir, build_dir = build_tree(
        tmp_path,
        {"harvest.md": "See [below](#papaya-yields).\n"},
        {page: '<h2 id="papaya-yields">Papaya yields</h2>'},
    )
    assert built_page(docs_dir / "harvest.md", docs_dir, build_dir) == build_dir / page
    assert check_anchors(docs_dir, build_dir).missing == []


def test_a_dotted_filename_keeps_its_name(tmp_path):
    """`repomatic.data.md` builds `repomatic.data.html`, not `repomatic.html`.

    Reading the stem with `Path.with_suffix` would take `.data` for the
    extension and look for the wrong page, which on this project's API pages
    is most of them.
    """
    docs_dir, build_dir = build_tree(
        tmp_path,
        {"fruit.harvest.md": "See [below](#papaya-yields).\n"},
        {"fruit.harvest.html": '<h2 id="papaya-yields">Papaya yields</h2>'},
    )
    assert (
        built_page(docs_dir / "fruit.harvest.md", docs_dir, build_dir)
        == build_dir / "fruit.harvest.html"
    )
    assert check_anchors(docs_dir, build_dir).missing == []


def test_theme_generated_anchors_are_never_validated(tmp_path):
    """Only what an author wrote is checked, so no denylist is needed.

    A rendered page is full of fragments nobody authored: footnote backrefs,
    header permalinks, sidebar targets. Reading links from the Markdown
    instead of from the HTML keeps every one of them out by construction.
    """
    docs_dir, build_dir = build_tree(
        tmp_path,
        {"harvest.md": "No links here at all.\n"},
        {
            "harvest.html": (
                '<a href="#id1">1</a><a href="#nowhere" class="headerlink">¶</a>'
                '<h2 id="papaya-yields">Papaya yields</h2>'
            )
        },
    )
    report = check_anchors(docs_dir, build_dir)
    assert report.missing == []
    assert report.checked == 0


def test_code_samples_are_not_links(tmp_path):
    """A fenced or inline example of a link documents one rather than making one."""
    source = (
        "Inline `](#in-code)` stays put.\n\n"
        "```markdown\n[fenced](#in-a-fence)\n```\n\n"
        "~~~\n[tilde](#in-a-tilde-fence)\n~~~\n\n"
        "But [this one](#real) counts.\n"
    )
    assert authored_fragments(source) == ["real"]

    docs_dir, build_dir = build_tree(
        tmp_path,
        {"harvest.md": source},
        {"harvest.html": '<h2 id="real">Real</h2>'},
    )
    assert check_anchors(docs_dir, build_dir).missing == []


def test_cross_page_links_are_left_to_sphinx(tmp_path):
    """A fragment behind a path is another page's anchor, and Sphinx resolves it."""
    assert authored_fragments("[x](other.md#frag) [y](https://e.example/#frag)") == []


def test_a_source_with_no_built_page_is_reported_not_failed(tmp_path):
    """An include-only fragment has no page of its own to be checked against.

    Failing on it would make the check an opinion about which sources belong
    in a toctree, which is the build's call and not this one's.
    """
    docs_dir, build_dir = build_tree(
        tmp_path,
        {"partial.md": "See [below](#papaya-yields).\n"},
        {"index.html": "<h1>Harvests</h1>"},
    )
    report = check_anchors(docs_dir, build_dir)
    assert report.missing == []
    assert report.unbuilt == [docs_dir / "partial.md"]


def test_the_rendered_site_is_not_scanned_as_a_source(tmp_path):
    """The build tree commonly sits inside the source tree, and is not prose.

    A Markdown file the build copied into its output would otherwise be read
    a second time, and reported against a page that does not exist.
    """
    docs_dir, build_dir = build_tree(
        tmp_path,
        {"harvest.md": "Nothing linked.\n"},
        {"_sources/harvest.md": "See [below](#never-checked).\n"},
    )
    (docs_dir / "_static").mkdir(parents=True, exist_ok=True)
    (docs_dir / "_static" / "notes.md").write_text(
        "See [below](#also-never-checked).\n", encoding="UTF-8"
    )
    assert list(markdown_sources(docs_dir, build_dir)) == [docs_dir / "harvest.md"]
    assert check_anchors(docs_dir, build_dir).checked == 0


def test_strip_code_preserves_line_count():
    """Blanking rather than deleting keeps every later line where it was."""
    source = "one\n```\ntwo\n```\nfour\n"
    assert strip_code(source).splitlines() == ["one", "", "", "", "four"]


def test_page_anchors_reads_id_and_name():
    """Both attributes are fragment targets, and both are used in the wild."""
    html = '<h2 id="from-id">x</h2><a name="from-name"></a><p class="not-an-anchor">'
    assert page_anchors(html) == {"from-id", "from-name"}
