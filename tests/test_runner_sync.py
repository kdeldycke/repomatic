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

Every case runs against a hand-built catalog rather than the live table, so the
suite never reaches the network and a GitHub restyle cannot turn these red for
a reason unrelated to the logic under test.
"""

from __future__ import annotations

import pytest

from repomatic.runner_catalog import RunnerImage
from repomatic.runner_images import (
    apply_axes_retirement,
    apply_retirement,
    apply_upgrade,
    plan_runner_changes,
    render_change_table,
)

CATALOG = [
    RunnerImage("Ubuntu 26.04", "x64", ("ubuntu-26.04",), True, False),
    RunnerImage("Ubuntu 24.04", "x64", ("ubuntu-24.04",), False, False),
    RunnerImage("Ubuntu 22.04", "x64", ("ubuntu-22.04",), False, True),
    RunnerImage("macOS 26", "x64", ("macos-26-intel",), False, False),
    # Same version as its sibling below, different toolchain: a flavour, not an
    # upgrade. This pair is why the rule keys on version rather than novelty.
    RunnerImage("Windows 11 Arm64", "arm64", ("windows-11-arm",), False, False),
    RunnerImage(
        "Windows 11 Arm64 with Visual Studio 2026",
        "arm64",
        ("windows-11-vs2026-arm",),
        True,
        False,
    ),
]


def test_deprecated_label_moves_to_a_released_successor() -> None:
    """A dying image moves onto released ground, and says where it is used."""
    (change,) = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]}, {"ubuntu-22.04"}, CATALOG
    )
    assert change.kind == "retirement"
    assert change.successor == "ubuntu-24.04"
    assert change.locations == ("tests.yaml:build",)
    assert "deprecated" in change.reason
    # 26.04 is newer but still in preview, so it is named rather than taken.
    assert change.alternative == "ubuntu-26.04"


def test_a_same_version_flavour_is_never_an_upgrade() -> None:
    """`windows-11-vs2026-arm` is a different toolchain, not a newer image.

    The distinction is the whole reason the upgrade rule keys on version: both
    rows are "Windows 11 Arm64", so novelty alone would propose a migration
    that buys nothing.
    """
    assert not plan_runner_changes({}, {"windows-11-arm"}, CATALOG)


def test_a_strictly_newer_version_is_proposed_as_a_probe() -> None:
    """An upgrade joins the matrix without anything being bet on it."""
    (change,) = plan_runner_changes({}, {"ubuntu-24.04"}, CATALOG)
    assert change.kind == "upgrade"
    assert change.successor == "ubuntu-26.04"
    assert change.locations == ()
    assert "supersedes" in change.reason


def test_running_the_newest_proposes_nothing() -> None:
    """The quiet steady state: nothing to say about a current fleet."""
    assert not plan_runner_changes({}, {"ubuntu-26.04", "macos-26-intel"}, CATALOG)


def test_ignored_labels_are_never_proposed() -> None:
    """A declined proposal stays declined across regenerations."""
    assert not plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04", "ubuntu-24.04"},
        CATALOG,
        ignore=["ubuntu-22.04", "ubuntu-26.04"],
    )


def test_an_unreadable_catalog_proposes_nothing() -> None:
    """Fail closed: no table means no successor can be trusted."""
    assert not plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]}, {"ubuntu-22.04"}, []
    )


def test_a_withdrawn_label_is_left_to_actionlint() -> None:
    """A label the table no longer carries is not reported here.

    `actionlint` already fails the Lint workflow on an unknown runner label,
    at the matrix axis as well as at a literal `runs-on:`, so a second report
    would duplicate a stronger check. Nothing here can name a replacement
    anyway: the row is gone, and its family and architecture with it.
    """
    assert not plan_runner_changes({}, {"ubuntu-18.04"}, CATALOG)


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
        {"ubuntu-22.04": ["tests.yaml:a"]}, {"ubuntu-22.04"}, CATALOG
    )

    assert [p.name for p in apply_retirement(change, workflows)] == ["tests.yaml"]
    written = (workflows / "tests.yaml").read_text(encoding="UTF-8")
    assert "runs-on: ubuntu-24.04" in written
    assert 'runs-on: "ubuntu-24.04"' in written
    assert "runs-on: ${{ matrix.os }}" in written
    assert not apply_retirement(change, workflows), "second run should be a no-op"


def test_apply_upgrade_writes_sections_not_inline_tables(tmp_path) -> None:
    """The probe lands as real sections and re-applies as a no-op."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="UTF-8")
    (change,) = plan_runner_changes({}, {"ubuntu-24.04"}, CATALOG)

    assert apply_upgrade(change, pyproject)
    written = pyproject.read_text(encoding="UTF-8")
    assert "[tool.repomatic.test-matrix]" in written
    assert "[tool.repomatic.test-matrix.variations]" in written
    assert 'os = ["ubuntu-26.04"]' in written
    assert not apply_upgrade(change, pyproject), "second run should be a no-op"


def test_apply_upgrade_is_idempotent_against_the_formatter_shape(tmp_path) -> None:
    """Re-reading what `format-pyproject` leaves behind finds the probe present.

    The applier writes sections; the formatter normalizes them into dotted keys
    under `[tool.repomatic]`, and tomlrt cannot emit that form itself. What
    stops the two rewriting each other on every push is that the dotted shape
    reads back as the same nested value, so a second apply writes nothing.

    The fixture is the formatter's real output.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\n\n'
        "[tool.repomatic]\n"
        'test-matrix.unstable = [ { os = "ubuntu-26.04" } ]\n'
        'test-matrix.variations.os = [ "ubuntu-26.04" ]\n',
        encoding="UTF-8",
    )
    (change,) = plan_runner_changes({}, {"ubuntu-24.04"}, CATALOG)

    assert not apply_upgrade(change, pyproject), (
        "the probe is already declared in the formatter's dotted-key shape, so "
        "re-applying must write nothing: otherwise sync-runner-images and "
        "format-pyproject each undo the other, forever"
    )


def test_apply_axes_retirement_moves_the_curated_tuple(tmp_path) -> None:
    """The axes move as text, keeping their comments and their ordering."""
    axes = tmp_path / "matrix_axes.py"
    axes.write_text(
        "TEST_RUNNERS_FULL = (\n"
        '    "ubuntu-22.04",  # the x86 slot\n'
        '    "macos-26-intel",\n'
        ")\n",
        encoding="UTF-8",
    )
    (change,) = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:a"]}, {"ubuntu-22.04"}, CATALOG
    )

    assert apply_axes_retirement(change, axes)
    written = axes.read_text(encoding="UTF-8")
    assert '"ubuntu-24.04",  # the x86 slot' in written
    assert not apply_axes_retirement(change, axes), "second run should be a no-op"


def test_change_table_carries_the_reasoning() -> None:
    """The body says why, not just what, and names what was passed over."""
    changes = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04", "ubuntu-24.04"},
        CATALOG,
    )
    table = render_change_table(changes)
    # Retirements first: one carries a deadline, the other an opportunity.
    assert table.index("🔴 retirement") < table.index("🆕 probe")
    assert "is deprecated" in table
    assert "`ubuntu-26.04` (preview)" in table


@pytest.mark.parametrize("kind", ["retirement", "upgrade"])
def test_every_change_explains_itself(kind: str) -> None:
    """No proposal is made without a reason a reviewer can act on."""
    changes = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04", "ubuntu-24.04"},
        CATALOG,
    )
    for change in changes:
        if change.kind == kind:
            assert change.reason
            assert change.successor
