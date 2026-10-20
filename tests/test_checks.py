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

"""Tests for :mod:`repomatic.checks`."""

from __future__ import annotations

import pytest

from repomatic.checks import CheckResult, report_result
from repomatic.github.outputs import AnnotationLevel


def test_check_result_unpacks_as_a_pair():
    """A result reads as `(passed, message)`, by name or by position."""
    passed, message = CheckResult(None, "Fig check: skipped.")
    assert passed is None
    assert message == "Fig check: skipped."


@pytest.mark.parametrize(
    ("result", "line"),
    (
        pytest.param(CheckResult(True, "Fig ripe."), "✓ Fig ripe.\n", id="pass"),
        pytest.param(
            CheckResult(None, "Fig check: skipped."),
            "ℹ Fig check: skipped.\n",
            id="skip",
        ),
    ),
)
def test_report_result_prints_without_annotating(result, line, capsys):
    """A pass or a skip is one terminal line, and never an annotation."""
    assert report_result(result) is False
    assert capsys.readouterr().out == line


@pytest.mark.parametrize(
    ("level", "line"),
    (
        pytest.param(
            AnnotationLevel.WARNING,
            "::warning::Fig rotten.\n⚠ Fig rotten.\n",
            id="warning",
        ),
        pytest.param(
            AnnotationLevel.ERROR,
            "::error::Fig rotten.\n✗ Fig rotten.\n",
            id="error",
        ),
    ),
)
def test_report_result_annotates_a_failure(level, line, capsys):
    """A failure is annotated at its level, then printed with its glyph."""
    assert report_result(CheckResult(False, "Fig rotten."), level) is True
    assert capsys.readouterr().out == line
