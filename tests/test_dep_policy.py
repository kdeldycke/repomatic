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

from repomatic.dep_policy import scan_policy

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
    """An unparseable entry is somebody else's error to report."""
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


def test_this_repository_follows_its_own_policy():
    """The canonical reference has to pass the rules it ships.

    Downstream repos mirror this file's conventions, so a finding here is
    either real drift or a rule too strict to be worth enforcing.
    """
    findings = scan_policy(PROJECT_ROOT / "pyproject.toml")
    assert findings == [], "\n".join(finding.message for finding in findings)
