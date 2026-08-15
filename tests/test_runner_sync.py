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

"""`sync-runner-images` proposes the right change, and only when there is one.

Every case here runs against a hand-built catalog rather than the live table,
so the suite never reaches the network and a GitHub restyle cannot turn these
red for a reason that has nothing to do with the logic under test.
"""

from __future__ import annotations

import pytest

from repomatic.runner_catalog import RunnerImage
from repomatic.runner_images import (
    Announcement,
    apply_arrival,
    apply_axes_retirement,
    apply_retirement,
    plan_runner_changes,
    render_change_table,
)

CATALOG = [
    RunnerImage("Ubuntu 26.04", "x64", ("ubuntu-26.04",), True, False, ""),
    RunnerImage("Ubuntu 24.04", "x64", ("ubuntu-24.04",), False, False, ""),
    RunnerImage("Ubuntu 22.04", "x64", ("ubuntu-22.04",), False, True, ""),
    RunnerImage("macOS 26", "x64", ("macos-26-intel",), False, False, ""),
]

RETIREMENT_BODY = """\
### Possible impact

Workflows using the `ubuntu-22.04` image label will be terminated.

### Target date

Deprecation: September 17th, 2026
Retirement: April 17th, 2027
"""

ARRIVAL_BODY = """\
### Possible impact

Workflows dependent on behaviours specific to Ubuntu 24 may be affected.

### Runner images affected

- [ ] Ubuntu 24.04
- [x] Ubuntu 26.04

### Target date

Thursday, June 11, 2026
"""


def announcement(title: str, body: str, number: int = 1) -> Announcement:
    return Announcement(
        number=number,
        title=title,
        url=f"https://github.com/actions/runner-images/issues/{number}",
        created_at="2026-06-16T00:00:00Z",
        body=body,
    )


RETIREMENT = announcement("[Ubuntu] Ubuntu 22 will begin deprecation", RETIREMENT_BODY)
ARRIVAL = announcement("[Ubuntu] Ubuntu 26.04 is now available", ARRIVAL_BODY, 2)


def test_retirement_moves_onto_the_released_successor() -> None:
    """A dying label moves to the newest released image, never to a preview."""
    (change,) = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04"},
        [RETIREMENT],
        CATALOG,
    )
    assert change.kind == "retirement"
    assert change.successor == "ubuntu-24.04"
    assert change.locations == ("tests.yaml:build",)
    assert "September 17th" in change.target_date


def test_arrival_is_proposed_only_for_a_preview_in_a_family_in_use() -> None:
    """A probe needs a genuinely new image, on a platform already carried."""
    (change,) = plan_runner_changes(
        {}, {"ubuntu-24.04"}, [ARRIVAL], CATALOG
    )
    assert change.kind == "arrival"
    assert change.label == "ubuntu-26.04"

    # No Ubuntu job at all: nothing to probe, since every probe cell costs
    # runner minutes on a capped pool.
    assert not plan_runner_changes({}, {"macos-26-intel"}, [ARRIVAL], CATALOG)


def test_a_generally_available_image_is_not_an_arrival() -> None:
    """"Not a deprecation" is not the same as "an arrival".

    A notice about a tooling default on a GA image would otherwise propose
    probing an image that shipped months ago.
    """
    tooling = announcement(
        "[macOS] Default Xcode on macOS 26 will change",
        "### Possible impact\n\nWorkflows using `macos-26-intel` are affected.\n",
        3,
    )
    assert not plan_runner_changes({}, {"ubuntu-24.04"}, [tooling], CATALOG)


def test_ignored_labels_are_never_proposed() -> None:
    """A declined proposal stays declined across regenerations."""
    assert not plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04"},
        [RETIREMENT, ARRIVAL],
        CATALOG,
        ignore=["ubuntu-22.04", "ubuntu-26.04"],
    )


def test_an_unreadable_catalog_proposes_nothing() -> None:
    """Fail closed: no table means no successor can be trusted."""
    assert not plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]}, {"ubuntu-22.04"}, [RETIREMENT], []
    )


def test_apply_retirement_rewrites_literals_only(tmp_path) -> None:
    """Quoting survives, expressions are left alone, and a re-run is a no-op."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "tests.yaml").write_text(
        "jobs:\n"
        "  a:\n    runs-on: ubuntu-22.04\n"
        '  b:\n    runs-on: "ubuntu-22.04"\n'
        "  c:\n    runs-on: ${{ matrix.os }}\n",
        encoding="UTF-8",
    )
    (change,) = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:a"]}, {"ubuntu-22.04"}, [RETIREMENT], CATALOG
    )

    assert [p.name for p in apply_retirement(change, workflows)] == ["tests.yaml"]
    written = (workflows / "tests.yaml").read_text(encoding="UTF-8")
    assert "runs-on: ubuntu-24.04" in written
    assert 'runs-on: "ubuntu-24.04"' in written
    assert "runs-on: ${{ matrix.os }}" in written
    assert not apply_retirement(change, workflows), "second run should be a no-op"


def test_apply_arrival_writes_sections_not_inline_tables(tmp_path) -> None:
    """The probe lands as real sections, which `format-pyproject` leaves alone.

    An inline table would be expanded back into sections by the formatter, and
    the two would rewrite each other on every push.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="UTF-8")
    (change,) = plan_runner_changes({}, {"ubuntu-24.04"}, [ARRIVAL], CATALOG)

    assert apply_arrival(change, pyproject)
    written = pyproject.read_text(encoding="UTF-8")
    assert "[tool.repomatic.test-matrix]" in written
    assert "[tool.repomatic.test-matrix.variations]" in written
    assert 'os = ["ubuntu-26.04"]' in written
    assert not apply_arrival(change, pyproject), "second run should be a no-op"


def test_apply_axes_retirement_moves_the_curated_tuple(tmp_path) -> None:
    """The axes move as text, keeping their comments and their ordering."""
    axes = tmp_path / "matrix_axes.py"
    axes.write_text(
        'TEST_RUNNERS_FULL = (\n'
        '    "ubuntu-22.04",  # the x86 slot\n'
        '    "macos-26-intel",\n'
        ')\n',
        encoding="UTF-8",
    )
    (change,) = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:a"]}, {"ubuntu-22.04"}, [RETIREMENT], CATALOG
    )

    assert apply_axes_retirement(change, axes)
    written = axes.read_text(encoding="UTF-8")
    assert '"ubuntu-24.04",  # the x86 slot' in written
    assert not apply_axes_retirement(change, axes), "second run should be a no-op"


def test_change_table_carries_the_evidence() -> None:
    """The body names the deadline and the announcement, not just the edit."""
    changes = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04"},
        [RETIREMENT, ARRIVAL],
        CATALOG,
    )
    table = render_change_table(changes)
    # Retirements first: one carries a deadline, the other an opportunity.
    assert table.index("🔴 retirement") < table.index("🆕 probe")
    assert "September 17th" in table
    assert "issues/1" in table


@pytest.mark.parametrize("kind", ["retirement", "arrival"])
def test_every_change_names_its_announcement(kind: str) -> None:
    """No proposal is made without a link to what justifies it."""
    changes = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04", "ubuntu-24.04"},
        [RETIREMENT, ARRIVAL],
        CATALOG,
    )
    for change in changes:
        if change.kind == kind:
            assert change.announcement_url.startswith("https://github.com/")
            assert change.announcement_title
