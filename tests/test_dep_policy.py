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

"""Tests for the dependency-declaration policy checks."""

from __future__ import annotations

from textwrap import dedent

import pytest

from repomatic.config import LintDepsConfig
from repomatic.dep_policy import count_comment_words, scan_policy

from .conftest import PROJECT_ROOT


def write_pyproject(tmp_path, body: str):
    """Write a `pyproject.toml` holding *body* and return its path."""
    path = tmp_path / "pyproject.toml"
    path.write_text(dedent(body), encoding="UTF-8")
    return path


def remedies(findings) -> list[str]:
    """The remedy of each finding, for a terse assertion."""
    return [finding.remedy for finding in findings]


def test_clean_declaration_reports_nothing(tmp_path):
    """A list that follows every rule produces no finding."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # boltons 25.0 dropped Python 3.9, matching our requires-python.
          "boltons>=25",
          # wcmatch 10.0 changed globbing semantics.
          "wcmatch>=10",
        ]
        """,
    )
    assert scan_policy(path) == []


def test_upper_bound_on_a_runtime_dependency(tmp_path):
    """A cap propagates to everyone installing this project."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # Pinned during the melon migration.
          "requests<3",
        ]
        """,
    )
    (finding,) = scan_policy(path)
    assert finding.package == "requests"
    assert "Drop the upper bound" in finding.remedy


@pytest.mark.parametrize("specifier", ("<3", "<=2.9", "==2.31", "~=2.31", "!=2.30"))
def test_every_capping_operator_is_caught(tmp_path, specifier):
    """`~=` counts: it implies a ceiling even without a `<` in it."""
    path = write_pyproject(
        tmp_path,
        f"""\
        [project]
        name = "orchard"
        dependencies = [
          # Comment, so only the cap is flagged.
          "requests{specifier}",
        ]
        """,
    )
    assert "Drop the upper bound" in remedies(scan_policy(path))[0]


def test_upper_bound_is_allowed_outside_runtime(tmp_path):
    """A dev group may pin harder: nothing downstream inherits it."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"

        [dependency-groups]
        # Pinned to keep CI reproducible.
        test = [
          "pytest~=8.0",
        ]
        """,
    )
    assert scan_policy(path) == []


def test_bare_dependency_is_flagged(tmp_path):
    """No specifier means the tested version and the installed one can differ."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # No floor at all.
          "requests",
        ]
        """,
    )
    assert "Add a `>=` floor" in remedies(scan_policy(path))[0]


def test_extra_of_an_already_floored_package_is_not_bare(tmp_path):
    """`click-extra[sphinx]` selects an extra, it does not re-declare a floor.

    Repeating the floor on the second mention would just be a second number
    to keep in step with the first.
    """
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # Floored once, here.
          "click-extra>=8.8",
        ]

        [dependency-groups]
        # Pulls the sphinx extra of the runtime dependency above.
        docs = [
          "click-extra[sphinx]",
        ]
        """,
    )
    assert scan_policy(path) == []


def test_aggregate_extra_selecting_the_project_itself_is_not_bare(tmp_path):
    """An `all` extra rolling up the project's own extras can carry no floor.

    A project never declares itself with a specifier in its own file, so the
    "floored somewhere else" gate can never catch it, and the resolver picks
    the version being installed: there is no other release to exclude.
    """
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        optional-dependencies.toml = [
          # tomlkit.dumps() renders the harvest tables.
          "tomlkit>=0.13",
        ]
        optional-dependencies.xml = [
          # xmltodict.unparse() renders the harvest tables.
          "xmltodict>=1",
        ]
        optional-dependencies.all = [
          "orchard[toml,xml]",
        ]
        """,
    )
    assert scan_policy(path) == []


def test_unsorted_list_names_only_the_first_offender(tmp_path):
    """One misplaced entry makes every later one look wrong too."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        # One block documenting the whole list.
        dependencies = [
          "boltons>=25",
          "zzz-late>=1",
          "apple>=2",
          "banana>=3",
        ]
        """,
    )
    (finding,) = scan_policy(path)
    assert finding.package == "apple"
    assert "Sort the list" in finding.remedy


def test_type_stub_outside_the_typing_group(tmp_path):
    """A stub reaching a runtime environment is pure weight."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"

        [dependency-groups]
        # Documented as a block.
        test = [
          "pytest>=8",
          "types-boltons>=25.0",
        ]
        """,
    )
    (finding,) = scan_policy(path)
    assert finding.package == "types-boltons"
    assert "typing" in finding.remedy


def test_type_stub_inside_the_typing_group_is_fine(tmp_path):
    """Where they belong, stubs draw no finding."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"

        [dependency-groups]
        # Documented as a block.
        typing = [
          "types-boltons>=25.0",
        ]
        """,
    )
    assert scan_policy(path) == []


def test_uncommented_floor_is_flagged(tmp_path):
    """A floor nobody justified cannot be audited later."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          "boltons>=25",
        ]
        """,
    )
    assert "Add a comment above it" in remedies(scan_policy(path))[0]


def test_a_comment_above_the_array_documents_the_whole_group(tmp_path):
    """A run of related entries is usually justified in one block."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"

        [dependency-groups]
        # types-boltons and types-pyyaml cover the stubs mypy needs.
        typing = [
          "types-boltons>=25.0",
          "types-pyyaml>=6.0",
        ]
        """,
    )
    assert scan_policy(path) == []


def test_inline_array_skips_the_comment_check(tmp_path):
    """An inline list has no per-entry line to hang a comment on."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = ["boltons>=25"]
        """,
    )
    assert scan_policy(path) == []


def test_missing_file_reports_nothing(tmp_path):
    """A non-Python repo has nothing to check, and must not raise."""
    assert scan_policy(tmp_path / "pyproject.toml") == []


def test_malformed_requirement_is_skipped(tmp_path):
    """An unparsable entry is somebody else's error to report."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # Not a PEP 508 requirement.
          "=== nonsense ===",
        ]
        """,
    )
    assert scan_policy(path) == []


@pytest.mark.parametrize(
    ("comment", "words"),
    (
        (["A floor comment."], 3),
        (["Split over", "two lines."], 4),
        # A URL is one word, and inline code counts as written.
        (["See https://example.com/a/b and `humanize()`."], 4),
        ([], 0),
    ),
)
def test_comment_word_count_ignores_markers(comment, words):
    """The `#` is stripped before counting; everything else counts."""
    assert count_comment_words(comment) == words


def test_over_long_floor_comment_is_flagged(tmp_path):
    """A comment that narrates every past floor buries the current one."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # papaya 3.0 ships the peel() the harvest report calls. Earlier floors
          # remain in play: 2.4 shipped slice(), which the crate packer used
          # before the rewrite; 2.0 renamed the ripeness scale the orchard map
          # reads; 1.7 fixed the stone counter that the yield estimate consumed
          # until the seed table replaced it.
          "papaya>=3",
        ]
        """,
    )
    (finding,) = scan_policy(path, comment_word_threshold=40)
    assert finding.package == "papaya"
    assert "documented in 50 words" in finding.detail
    assert "Superseded floors" in finding.remedy


def test_a_short_floor_comment_passes(tmp_path):
    """The rule caps the narration, not the justification."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # papaya 3.0 ships the peel() the harvest report calls.
          "papaya>=3",
        ]
        """,
    )
    assert scan_policy(path, comment_word_threshold=40) == []


def test_comment_length_is_unchecked_by_default(tmp_path):
    """A caller with no configuration to read gets the presence check alone."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # papaya 3.0 ships the peel() the harvest report calls, and this
          # comment runs on well past any threshold a project would set, for
          # the sake of counting more words than a reader would ever want to
          # read about one single dependency floor in one single place.
          "papaya>=3",
        ]
        """,
    )
    assert scan_policy(path) == []


def test_a_blank_line_detaches_the_comment_above(tmp_path):
    """An unattached comment leaves the floor undocumented."""
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        dependencies = [
          # papaya 3.0 ships the peel() the harvest report calls.

          "papaya>=3",
        ]
        """,
    )
    assert "Add a comment above it" in remedies(scan_policy(path))[0]


def test_a_block_comment_excuses_absence_but_not_length(tmp_path):
    """A preamble above the array would otherwise silence every entry.

    The escape hatch exists so a run of related entries can be justified in
    one block. Reading it as a blanket pass makes any project whose array
    carries a policy preamble unscannable, which is how
    `meta-package-manager` accumulated a 676-word floor comment.
    """
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        # Every floor below is documented, and none is capped from above.
        dependencies = [
          "mango>=2",
          # papaya 3.0 ships the peel() the harvest report calls. Earlier floors
          # remain in play: 2.4 shipped slice(), which the crate packer used
          # before the rewrite; 2.0 renamed the ripeness scale the orchard map
          # reads; 1.7 fixed the stone counter that the yield estimate consumed
          # until the seed table replaced it.
          "papaya>=3",
        ]
        """,
    )
    (finding,) = scan_policy(path, comment_word_threshold=40)
    assert finding.package == "papaya"
    assert "documented in 50 words" in finding.detail


def test_a_wall_moved_above_the_array_is_still_a_wall(tmp_path):
    """Moving the comment up one line must not buy an exemption.

    Reported once, against the first entry leaning on the block, rather than
    once per entry: it is one comment to rewrite.
    """
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        # papaya 3.0 ships the peel() the harvest report calls. Earlier floors
        # remain in play: 2.4 shipped slice(), which the crate packer used
        # before the rewrite; 2.0 renamed the ripeness scale the orchard map
        # reads; 1.7 fixed the stone counter that the yield estimate consumed
        # until the seed table replaced it.
        dependencies = [
          "mango>=2",
          "papaya>=3",
        ]
        """,
    )
    (finding,) = scan_policy(path, comment_word_threshold=40)
    assert finding.package == "mango"
    assert "50-word block above the array" in finding.detail


def test_an_array_preamble_documenting_no_floor_is_not_measured(tmp_path):
    """A version-policy header justifies nothing, so length is not its measure.

    Every entry below carries its own comment, so the block is a preamble.
    Flagging it would report a rule about floor comments against a comment
    that documents no floor.
    """
    path = write_pyproject(
        tmp_path,
        """\
        [project]
        name = "orchard"
        # Floors use `>=`, never `~=`, so a packager can ship a security
        # hotfix without waiting on this project. Every floor below names the
        # API it needs, and none of them caps a version from above, since a
        # cap here propagates to everyone installing this orchard.
        dependencies = [
          # mango 2.0 ships the ripen() the harvest report calls.
          "mango>=2",
          # papaya 3.0 ships the peel() the crate packer calls.
          "papaya>=3",
        ]
        """,
    )
    assert scan_policy(path, comment_word_threshold=40) == []


def test_this_repository_follows_its_own_policy():
    """The canonical reference has to pass the rules it ships.

    Downstream repos mirror this file's conventions, so a finding here is
    either real drift or a rule too strict to be worth enforcing. Run at the
    configured default rather than at the disabled-by-default `0`, since the
    threshold is exactly the rule most likely to drift back.
    """
    findings = scan_policy(
        PROJECT_ROOT / "pyproject.toml",
        LintDepsConfig.comment_word_threshold,
    )
    assert findings == [], "\n".join(finding.message for finding in findings)
