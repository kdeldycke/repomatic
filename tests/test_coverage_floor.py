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

"""Tests for `lint-coverage`: the coverage floor against the coverage total."""

from __future__ import annotations

import json

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.ci import COVERAGE_FLOOR_SLACK
from repomatic.cli.main import repomatic

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty project directory, as the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _lint_coverage(
    project: Path, floor: float | None, total: float | None, *args: str
) -> Result:
    """Run `lint-coverage` against a floor and a report total.

    :param project: The working directory to write the two files in.
    :param floor: The `report.fail_under` value, or `None` to declare none.
    :param total: The total of `coverage.json`, or `None` to write no report.
    :param args: Extra arguments of the command.
    """
    config = '[project]\nname = "papaya"\n'
    if floor is not None:
        config += f"\n[tool.coverage]\nreport.fail_under = {floor}\n"
    (project / "pyproject.toml").write_text(config, encoding="UTF-8")
    if total is not None:
        (project / "coverage.json").write_text(
            json.dumps({"totals": {"percent_covered": total}}), encoding="UTF-8"
        )
    return CliRunner().invoke(repomatic, ["lint-coverage", *args])


@pytest.mark.parametrize(
    ("headroom", "lags"),
    ((0.5, False), (COVERAGE_FLOOR_SLACK, False), (COVERAGE_FLOOR_SLACK + 0.01, True)),
)
def test_floor_lags_past_the_slack(project: Path, headroom: float, lags: bool) -> None:
    """A floor further under the total than the slack fails the check."""
    result = _lint_coverage(project, 90, 90 + headroom)

    assert result.exit_code == (1 if lags else 0), result.output
    assert ("✓ The 90% floor keeps within" in result.output) is not lags


@pytest.mark.parametrize(
    ("args", "exit_code", "annotation"),
    (([], 1, "::error"), (["--no-fatal"], 0, "::warning")),
)
def test_lagging_floor_warns_without_fatal(
    project: Path, args: list[str], exit_code: int, annotation: str
) -> None:
    """`--no-fatal` turns the failure into a warning, with the same message."""
    result = _lint_coverage(project, 80, 92.24, *args)

    assert result.exit_code == exit_code, result.output
    assert annotation in result.output
    assert "sits 12.24 points under the 92.24% coverage total" in result.output


@pytest.mark.parametrize(
    ("floor", "total", "message"),
    (
        (93, None, "No coverage report at"),
        (None, 94.2, "No [tool.coverage] report.fail_under floor"),
        (0, 94.2, "No [tool.coverage] report.fail_under floor"),
    ),
)
def test_nothing_to_compare_passes(
    project: Path, floor: float | None, total: float | None, message: str
) -> None:
    """With no report or no floor, the check passes and says why."""
    result = _lint_coverage(project, floor, total)

    assert result.exit_code == 0, result.output
    assert message in result.output


def test_unreadable_report_fails_cleanly(project: Path) -> None:
    """A report with no total ends on an error message, not a traceback."""
    (project / "coverage.json").write_text("{}", encoding="UTF-8")

    result = _lint_coverage(project, 93, None)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Cannot compare" in result.output
