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

"""Tests for the `test-plan` command: parallel execution and progress display.

The `--jobs` option is click-extra's `JobsOption`; these tests cover how
`test-plan` consumes its worker count, not the option's own validation. The
spinner tests check the integration (TTY gating, `--no-progress`), not the
`Spinner` animation itself, which click-extra exercises.
"""

from __future__ import annotations

import os
import sys

import pytest
from click.testing import CliRunner

from repomatic.cli import repomatic

# A case that passes: the host interpreter exits 0 on `--version`.
PASS_CASE = "- cli_parameters: --version\n  exit_code: 0"
# A case that fails: `--version` never exits 99, so the expectation mismatches.
FAIL_CASE = "- cli_parameters: --version\n  exit_code: 99"


def _run(tmp_path, jobs, *cases: str, stats: bool = True):
    """Invoke `test-plan` over an inline plan, optionally setting --jobs.

    `jobs=None` omits the flag, exercising the default (parallel) worker count.
    `stats=False` adds --no-stats to silence the run summary. The host Python
    interpreter stands in for the command under test, so cases stay fast and
    platform-neutral.
    """
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("\n".join(cases))
    args = ["test-plan", "--command", sys.executable, "--plan-file", str(plan_file)]
    if jobs is not None:
        args += ["--jobs", str(jobs)]
    if not stats:
        args.append("--no-stats")
    return CliRunner().invoke(repomatic, args, catch_exceptions=False)


# None exercises the default (parallel) count; 1 forces sequential; 3 is parallel.
@pytest.mark.parametrize("jobs", [None, 1, 3])
def test_every_passing_case_runs(tmp_path, jobs):
    """All cases pass identically whether run sequentially or in parallel."""
    result = _run(tmp_path, jobs, PASS_CASE, PASS_CASE, PASS_CASE)
    assert result.exit_code == 0
    assert "Total: 3" in result.output
    assert "Failed: 0" in result.output


@pytest.mark.parametrize("jobs", [1, 3])
def test_failure_is_counted_in_both_modes(tmp_path, jobs):
    """A failing case is detected and reported sequentially and in parallel."""
    result = _run(tmp_path, jobs, PASS_CASE, FAIL_CASE, PASS_CASE)
    assert result.exit_code == 1
    assert "Total: 3" in result.output
    assert "Failed: 1" in result.output


def test_non_integer_jobs_is_rejected(tmp_path):
    """--jobs is an integer option, so a non-numeric value is refused."""
    result = _run(tmp_path, "banana", PASS_CASE)
    assert result.exit_code != 0
    assert "banana" in result.stderr


def test_run_summary_reports_workers_and_cpu_count(tmp_path):
    """The run echoes the case count, the worker count, and os.cpu_count()."""
    result = _run(tmp_path, 3, PASS_CASE, PASS_CASE)
    assert result.exit_code == 0
    assert "Running 2 test cases across 3 workers " in result.output
    assert f"(os.cpu_count()={os.cpu_count()})" in result.output


def test_no_stats_suppresses_run_summary(tmp_path):
    """--no-stats hides the worker summary alongside the results tally."""
    result = _run(tmp_path, 3, PASS_CASE, PASS_CASE, stats=False)
    assert result.exit_code == 0
    assert "os.cpu_count()" not in result.output
    assert "Test plan results" not in result.output


def test_progress_spinner_stays_off_non_tty(tmp_path):
    """Off a TTY (captured buffers) the spinner draws no frame to stdout or stderr."""
    result = _run(tmp_path, 3, PASS_CASE, PASS_CASE)
    assert result.exit_code == 0
    # The default Braille frame must never reach captured (non-TTY) output.
    assert "⠋" not in result.output
    assert "⠋" not in result.stderr


def test_no_progress_runs_clean(tmp_path):
    """--no-progress (a group option) silences the spinner without altering results."""
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(f"{PASS_CASE}\n{FAIL_CASE}")
    result = CliRunner().invoke(
        repomatic,
        ["--no-progress", "test-plan", "--command", sys.executable]
        + ["--plan-file", str(plan_file)],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "Total: 2" in result.output
    assert "Failed: 1" in result.output


def test_exit_on_error_bails_before_summary(tmp_path):
    """--exit-on-error stops at the first failure and skips the results summary,
    even with the spinner now wrapping the run."""
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(f"{FAIL_CASE}\n{PASS_CASE}\n{PASS_CASE}")
    result = CliRunner().invoke(
        repomatic,
        ["test-plan", "--command", sys.executable, "--plan-file", str(plan_file)]
        + ["--jobs", "1", "--exit-on-error"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "Test plan results" not in result.output
