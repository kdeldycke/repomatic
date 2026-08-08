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

"""Tests for the removal of the ToC entries awesome-lint forbids."""

from __future__ import annotations

import pytest

from repomatic.awesome_toc import (
    FORBIDDEN_HEADINGS,
    README_RE,
    fix_awesome_toc,
    forbidden_headings_for,
    headings,
    strip_toc_entries,
)

ENGLISH_SECTIONS = (
    "Contents",
    "Mango",
    "Papaya",
    "Lychee",
    "Contributing",
    "Footnotes",
)
"""Section titles of the reference readme, in document order."""

FRENCH_SECTIONS = ("Sommaire", "Mangue", "Papaye", "Litchi", "Contribuer", "Notes")
"""The same sections, translated. Only positions tie one list to the other."""


def render_readme(sections: tuple[str, ...]) -> str:
    """Render a readme whose ToC lists every section, as mdformat-toc leaves it."""
    entries = "\n".join(f"- [{title}](#{title.lower()})" for title in sections)
    body = "\n\n".join(f"## {title}\n\nSome prose." for title in sections[1:])
    return (
        f"## {sections[0]}\n\n"
        "<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->"
        f"\n\n{entries}\n\n"
        "<!-- mdformat-toc end -->\n\n"
        f"{body}\n"
    )


def toc_entries(content: str) -> list[str]:
    """List the link texts of a readme's ToC block."""
    block = content.split("mdformat-toc start")[1].split("mdformat-toc end")[0]
    return [
        line.strip().removeprefix("- [").split("](")[0]
        for line in block.splitlines()
        if line.strip().startswith("- [")
    ]


@pytest.mark.parametrize(
    ("filename", "matches"),
    (
        ("readme.md", True),
        ("readme.zh.md", True),
        ("readme.pt-br.md", True),
        ("readme.txt", False),
        ("readmemd", False),
        ("readme.zh.rst", False),
        ("contributing.md", False),
        ("my-readme.md", False),
    ),
)
def test_readme_re(filename, matches):
    """Only a root readme and its translations are rewritten."""
    assert bool(README_RE.match(filename)) is matches


def test_headings_skips_fenced_code():
    """A `#` line inside a code fence is a comment, not a heading."""
    content = (
        "# Recipe\n\n"
        "```bash\n"
        "# Peel the mango first.\n"
        "peel --fruit mango\n"
        "```\n\n"
        "## Papaya ##\n"
    )
    assert headings(content) == ["Recipe", "Papaya"]


def test_strip_toc_entries_leaves_the_rest_of_the_document_alone():
    """A list item outside the ToC block is not a ToC entry."""
    content = render_readme(ENGLISH_SECTIONS) + "\n- [Contributing](#contributing)\n"
    updated, removed = strip_toc_entries(content, {"Contributing"})

    assert removed == ["Contributing"]
    assert updated.endswith("- [Contributing](#contributing)\n")


def test_strip_toc_entries_without_a_toc_block():
    """A readme carrying no ToC marker is returned untouched."""
    content = "## Mango\n\n- [Contents](#contents)\n"
    assert strip_toc_entries(content, set(FORBIDDEN_HEADINGS)) == (content, [])


def test_forbidden_headings_for_maps_positions_onto_a_translation():
    """A translated section is forbidden when its English peer is."""
    forbidden = forbidden_headings_for(
        render_readme(FRENCH_SECTIONS), list(ENGLISH_SECTIONS)
    )
    assert forbidden == {*FORBIDDEN_HEADINGS, "Sommaire", "Contribuer", "Notes"}


def test_forbidden_headings_for_falls_back_on_a_count_mismatch(caplog):
    """A translation that dropped a section is matched on English names only."""
    forbidden = forbidden_headings_for(
        render_readme(FRENCH_SECTIONS[:-1]), list(ENGLISH_SECTIONS)
    )
    assert forbidden == set(FORBIDDEN_HEADINGS)
    assert "Heading count differs" in caplog.text


def test_fix_awesome_toc_strips_reference_and_translation(tmp_path):
    """The reported bug: a translated ToC keeps the entries the English one loses."""
    (tmp_path / "readme.md").write_text(
        render_readme(ENGLISH_SECTIONS), encoding="utf-8"
    )
    (tmp_path / "readme.fr.md").write_text(
        render_readme(FRENCH_SECTIONS), encoding="utf-8"
    )

    report = fix_awesome_toc(tmp_path)

    assert report[tmp_path / "readme.md"] == ["Contents", "Contributing", "Footnotes"]
    assert report[tmp_path / "readme.fr.md"] == ["Sommaire", "Contribuer", "Notes"]
    assert toc_entries((tmp_path / "readme.md").read_text(encoding="utf-8")) == [
        "Mango",
        "Papaya",
        "Lychee",
    ]
    assert toc_entries((tmp_path / "readme.fr.md").read_text(encoding="utf-8")) == [
        "Mangue",
        "Papaye",
        "Litchi",
    ]


def test_fix_awesome_toc_matches_an_untranslated_section_by_name(tmp_path):
    """A section left in English is still caught, positions or not."""
    mixed = ("Contents", "Mangue", "Papaye", "Litchi", "Contribuer", "Footnotes")
    (tmp_path / "readme.md").write_text(
        render_readme(ENGLISH_SECTIONS), encoding="utf-8"
    )
    (tmp_path / "readme.fr.md").write_text(render_readme(mixed), encoding="utf-8")

    report = fix_awesome_toc(tmp_path)

    assert report[tmp_path / "readme.fr.md"] == [
        "Contents",
        "Contribuer",
        "Footnotes",
    ]


def test_fix_awesome_toc_is_idempotent(tmp_path):
    """Re-running finds nothing left to remove."""
    (tmp_path / "readme.md").write_text(
        render_readme(ENGLISH_SECTIONS), encoding="utf-8"
    )
    (tmp_path / "readme.fr.md").write_text(
        render_readme(FRENCH_SECTIONS), encoding="utf-8"
    )

    assert fix_awesome_toc(tmp_path)
    before = (tmp_path / "readme.fr.md").read_text(encoding="utf-8")

    assert fix_awesome_toc(tmp_path) == {}
    assert (tmp_path / "readme.fr.md").read_text(encoding="utf-8") == before


def test_fix_awesome_toc_without_a_reference_readme(tmp_path, caplog):
    """A translation alone still loses the entries awesome-lint names."""
    mixed = ("Contents", "Mangue", "Papaye", "Litchi", "Contribuer", "Footnotes")
    (tmp_path / "readme.fr.md").write_text(render_readme(mixed), encoding="utf-8")

    report = fix_awesome_toc(tmp_path)

    assert report[tmp_path / "readme.fr.md"] == ["Contents", "Footnotes"]
    assert "No readme.md" in caplog.text


def test_fix_awesome_toc_rewrites_the_reference_first(tmp_path):
    """The reference is processed before any translation is compared to it."""
    for name in ("readme.md", "readme.fr.md", "readme.zh.md"):
        (tmp_path / name).write_text(render_readme(ENGLISH_SECTIONS), encoding="utf-8")

    assert [path.name for path in fix_awesome_toc(tmp_path)] == [
        "readme.md",
        "readme.fr.md",
        "readme.zh.md",
    ]
