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

"""The outcome of one check, and how a check run reports it.

The repository lint, the workflow lint and the token probes all answer the same
question about something they inspected: did it pass, did it fail, or could the
check not tell. One result type serves all of them, so a reporter renders any
of them the same way.
"""

from __future__ import annotations

from typing import NamedTuple

from click_extra import echo

from .github.outputs import AnnotationLevel, report_finding


class CheckResult(NamedTuple):
    """Outcome of one check.

    `passed` is tri-state: `True` on success, `False` on failure, `None` when
    the check could not run or does not apply (skipped). `message` is the
    human-readable line for both terminal output and annotations.
    """

    passed: bool | None
    message: str


def report_result(
    result: CheckResult, level: AnnotationLevel = AnnotationLevel.WARNING
) -> bool:
    """Print one check line, and annotate it when the check failed.

    A pass prints `✓` and a skip prints `ℹ`, neither with an annotation. A
    failure goes through {func}`~repomatic.github.outputs.report_finding` at
    *level*.

    :param result: The check outcome to render.
    :param level: Severity of a failure.
    :return: `True` when the check failed (`passed is False`), else `False`.
    """
    if result.passed is None:
        echo(f"ℹ {result.message}")
        return False
    if result.passed:
        echo(f"✓ {result.message}")
        return False
    report_finding(level, result.message)
    return True
