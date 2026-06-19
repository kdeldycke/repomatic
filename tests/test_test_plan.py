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

"""Tests for the `test-plan` command, focused on parallel execution via --jobs.

The `--jobs` option is click-extra's `JobsOption`; these tests cover how
`test-plan` consumes its worker count, not the option's own validation.
"""

from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from repomatic.cli import repomatic

# A case that passes: the host interpreter exits 0 on `--version`.
PASS_CASE = "- cli_parameters: --version\n  exit_code: 0"
# A case that fails: `--version` never exits 99, so the expectation mismatches.
FAIL_CASE = "- cli_parameters: --version\n  exit_code: 99"


def _run(tmp_path, jobs, *cases: str):
    """Invoke `test-plan` over an inline plan, optionally setting --jobs.

    `jobs=None` omits the flag, exercising the default (parallel) worker count.
    The host Python interpreter stands in for the command under test, so cases
    stay fast and platform-neutral.
    """
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("\n".join(cases))
    args = ["test-plan", "--command", sys.executable, "--plan-file", str(plan_file)]
    if jobs is not None:
        args += ["--jobs", str(jobs)]
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
