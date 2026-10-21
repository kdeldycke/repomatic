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

import ast
import runpy

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.matrix.axes import KNOWN_RUNNERS, LINUX_X64_RUNNER
from repomatic.matrix.runner_sync import (
    AXES_SOURCE_PATH,
    ChangeKind,
    RunnerChange,
    plan_runner_changes,
    render_change_table,
)
from repomatic.matrix.runners import RunnerCatalog, RunnerImage
from tests.conftest import PACKAGE_FILES, PROJECT_ROOT

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path

CATALOG = RunnerCatalog((
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
))


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


def test_an_upgrade_moves_the_jobs_naming_the_old_image() -> None:
    """A literal is the one `runs-on:` no probe reaches, so the upgrade moves it."""
    (change,) = plan_runner_changes(
        {"ubuntu-24.04": ["check-deps.yaml:build"]}, {"ubuntu-24.04"}, CATALOG
    )
    assert change.kind == "upgrade"
    assert change.successor == "ubuntu-26.04"
    assert change.locations == ("check-deps.yaml:build",)
    assert change.probe


def test_a_successor_already_in_the_fleet_is_moved_onto_but_never_probed(
    tmp_path,
) -> None:
    """One job left behind still moves, without un-gating the current image.

    A repository whose matrix already runs `ubuntu-26.04`, and which keeps a
    single job on `ubuntu-24.04`, gets that job moved: it is the half no probe
    can do. The probe half is dropped, since writing an `unstable` entry for an
    image already in the matrix marks *every* `ubuntu-26.04` cell
    `continue-on-error`, turning the repository's own gating column advisory.
    """
    (change,) = plan_runner_changes(
        {"ubuntu-24.04": ["check-deps.yaml:build"]},
        {"ubuntu-24.04", "ubuntu-26.04"},
        CATALOG,
    )
    assert change.locations == ("check-deps.yaml:build",)
    assert not change.probe

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "papaya"\n', encoding="UTF-8")
    assert not change.add_probe(pyproject)
    assert "test-matrix" not in pyproject.read_text(encoding="UTF-8")


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
        {"ubuntu-22.04": ["tests.yaml:build"]}, {"ubuntu-22.04"}, RunnerCatalog()
    )


def test_a_withdrawn_label_is_left_to_actionlint() -> None:
    """A label the table no longer carries is not reported here.

    `actionlint` already fails the Lint workflow on an unknown runner label,
    at the matrix axis as well as at a literal `runs-on:`, so a second report
    would duplicate a stronger check. Nothing here can name a replacement
    anyway: the row is gone, and its family and architecture with it.
    """
    assert not plan_runner_changes({}, {"ubuntu-18.04"}, CATALOG)


def test_apply_literal_rewrite_touches_literals_only(tmp_path) -> None:
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

    assert [p.name for p in change.rewrite_literals(workflows)] == ["tests.yaml"]
    written = (workflows / "tests.yaml").read_text(encoding="UTF-8")
    assert "runs-on: ubuntu-24.04" in written
    assert 'runs-on: "ubuntu-24.04"' in written
    assert "runs-on: ${{ matrix.os }}" in written
    assert not change.rewrite_literals(workflows), "second run should be a no-op"


def test_apply_upgrade_writes_sections_not_inline_tables(tmp_path) -> None:
    """The probe lands as real sections and re-applies as a no-op."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="UTF-8")
    (change,) = plan_runner_changes({}, {"ubuntu-24.04"}, CATALOG)

    assert change.add_probe(pyproject)
    written = pyproject.read_text(encoding="UTF-8")
    assert "[tool.repomatic.test-matrix]" in written
    assert "[tool.repomatic.test-matrix.variations]" in written
    assert 'os = ["ubuntu-26.04"]' in written
    assert not change.add_probe(pyproject), "second run should be a no-op"


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

    assert not change.add_probe(pyproject), (
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

    assert change.rewrite_axes(axes)
    written = axes.read_text(encoding="UTF-8")
    assert '"ubuntu-24.04",  # the x86 slot' in written
    assert not change.rewrite_axes(axes), "second run should be a no-op"


def _code_strings(path: Path) -> list[str]:
    """Every string literal of a module, docstrings left out."""
    tree = ast.parse(path.read_text(encoding="UTF-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.mark.parametrize("label", sorted(KNOWN_RUNNERS))
def test_runner_label_is_spelled_once(label: str) -> None:
    """Each runner label of the axes is one literal, in the axes module.

    A retirement rewrites that module alone, so a copy of the label anywhere
    else in the package stays on the retired image: the build fleet did, until
    a hand edit made the retirement pull request pass.
    """
    spelled = {
        path.relative_to(PROJECT_ROOT).as_posix(): count
        for path in PACKAGE_FILES
        if (count := _code_strings(path).count(label))
    }
    assert spelled == {AXES_SOURCE_PATH.as_posix(): 1}


def test_retirement_moves_every_reader_of_the_axes(tmp_path) -> None:
    """A retirement rewrite of the real axes module moves the slot it names.

    The test axes and the build fleet both read the slot constant, so the one
    literal the rewrite changes is all either of them sees.
    """
    axes = tmp_path / "axes.py"
    axes.write_text(
        (PROJECT_ROOT / AXES_SOURCE_PATH).read_text(encoding="UTF-8"),
        encoding="UTF-8",
    )
    change = RunnerChange(
        kind=ChangeKind.RETIREMENT,
        label=LINUX_X64_RUNNER,
        successor="ubuntu-28.04",
        locations=(),
        reason="Ubuntu 26.04 is deprecated",
        alternative="",
    )

    assert change.rewrite_axes(axes)

    moved = runpy.run_path(str(axes))
    assert moved["LINUX_X64_RUNNER"] == "ubuntu-28.04"
    assert "ubuntu-28.04" in moved["TEST_RUNNERS_FULL"]
    assert LINUX_X64_RUNNER not in moved["TEST_RUNNERS_FULL"]


def test_change_table_carries_the_reasoning() -> None:
    """The body says why, not just what, and names what was passed over."""
    changes = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:build"]},
        {"ubuntu-22.04", "ubuntu-24.04"},
        CATALOG,
    )
    table = render_change_table(changes)
    # Retirements first: one carries a deadline, the other an opportunity.
    assert table.index("🔴 retirement") < table.index("🆕 upgrade")
    assert "is deprecated" in table
    assert "`ubuntu-26.04` (preview)" in table


def test_change_table_names_both_halves_of_an_upgrade() -> None:
    """A reviewer sees the jobs that move and the cell that joins the matrix."""
    (change,) = plan_runner_changes(
        {"ubuntu-24.04": ["check-deps.yaml:build"]}, {"ubuntu-24.04"}, CATALOG
    )
    row = render_change_table([change])
    assert "`ubuntu-24.04` → `ubuntu-26.04` in `check-deps.yaml:build`" in row
    assert "joins the matrix as `continue-on-error`" in row


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


def test_apply_moves_the_axes_only_for_a_retirement(tmp_path) -> None:
    """A retirement moves the curated axes when given them, never the matrix probe."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "tests.yaml").write_text(
        "jobs:\n  a:\n    runs-on: ubuntu-22.04\n", encoding="UTF-8"
    )
    axes = tmp_path / "axes.py"
    axes.write_text('TEST_RUNNERS_FULL = ("ubuntu-22.04",)\n', encoding="UTF-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "papaya"\n', encoding="UTF-8")
    (change,) = plan_runner_changes(
        {"ubuntu-22.04": ["tests.yaml:a"]}, {"ubuntu-22.04"}, CATALOG
    )
    assert change.kind is ChangeKind.RETIREMENT

    assert change.apply(workflows, pyproject) == [workflows / "tests.yaml"]
    assert "ubuntu-22.04" in axes.read_text(encoding="UTF-8")

    (workflows / "tests.yaml").write_text(
        "jobs:\n  a:\n    runs-on: ubuntu-22.04\n", encoding="UTF-8"
    )
    assert change.apply(workflows, pyproject, axes) == [
        workflows / "tests.yaml",
        axes,
    ]
    assert "test-matrix" not in pyproject.read_text(encoding="UTF-8")


def test_apply_adds_the_probe_only_for_an_upgrade(tmp_path) -> None:
    """An upgrade lands its matrix probe, and leaves the curated axes alone."""
    axes = tmp_path / "axes.py"
    axes.write_text('TEST_RUNNERS_FULL = ("ubuntu-24.04",)\n', encoding="UTF-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "papaya"\n', encoding="UTF-8")
    (change,) = plan_runner_changes({}, {"ubuntu-24.04"}, CATALOG)
    assert change.kind is ChangeKind.UPGRADE

    assert change.apply(tmp_path, pyproject, axes) == [pyproject]
    assert "ubuntu-24.04" in axes.read_text(encoding="UTF-8")


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        (ChangeKind.RETIREMENT, "🔴 retirement"),
        (ChangeKind.UPGRADE, "🆕 upgrade"),
    ],
)
def test_change_kind_labels(kind: ChangeKind, label: str) -> None:
    """Each kind carries the cell the pull request table shows for it."""
    assert kind.label == label
    assert label.endswith(str(kind))


def test_retirements_rank_first() -> None:
    """A retirement carries a deadline, so it sorts before an upgrade."""
    assert ChangeKind.RETIREMENT.rank < ChangeKind.UPGRADE.rank


@pytest.mark.parametrize("dry_run", [True, False])
def test_sync_runner_images_command(tmp_path, monkeypatch, dry_run) -> None:
    """The command proposes each change, and makes it unless asked not to."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "tests.yaml"
    workflow.write_text(
        "jobs:\n  build:\n    runs-on: ubuntu-22.04\n    steps: []\n",
        encoding="UTF-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "papaya"\n', encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("repomatic.cli.ci.fetch_catalog", lambda: CATALOG)

    args = ["sync-runner-images", "--output", "proposal.md"]
    result = CliRunner().invoke(
        repomatic, [*args, "--dry-run" if dry_run else "--live"]
    )

    assert result.exit_code == 0, result.output
    assert "retirement: `ubuntu-22.04` → `ubuntu-24.04`" in result.output
    assert "🔴 retirement" in (tmp_path / "proposal.md").read_text(encoding="UTF-8")
    written = workflow.read_text(encoding="UTF-8")
    assert ("runs-on: ubuntu-22.04" in written) is dry_run
    assert ("runs-on: ubuntu-24.04" in written) is not dry_run


def test_sync_runner_images_switched_off(tmp_path, monkeypatch) -> None:
    """`sync-runner-images.sync = false` reads no table and moves no job.

    The proposal is still written, empty: the `pr-sync` step after the command
    reads it whatever the command decides.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "tests.yaml"
    workflow.write_text(
        "jobs:\n  build:\n    runs-on: ubuntu-22.04\n    steps: []\n",
        encoding="UTF-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "papaya"\n\n'
        "[tool.repomatic]\nsync-runner-images.sync = false\n",
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    def unreachable():
        raise AssertionError("a switched-off sync reads no image table")

    monkeypatch.setattr("repomatic.cli.ci.fetch_catalog", unreachable)

    result = CliRunner().invoke(
        repomatic, ["sync-runner-images", "--output", "proposal.md"]
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "proposal.md").read_text(encoding="UTF-8") == "\n"
    assert "runs-on: ubuntu-22.04" in workflow.read_text(encoding="UTF-8")
