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

"""Tests that --help renders without errors for every CLI command."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from repomatic.cli import repomatic


def _collect_commands(
    group: click.Group | click.Command,
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    """Recursively collect all command paths from a Click group."""
    paths: list[tuple[str, ...]] = [prefix] if prefix else [()]
    if isinstance(group, click.Group):
        for name in sorted(group.list_commands(click.Context(group))):
            cmd = group.get_command(click.Context(group), name)
            if cmd is None:
                continue
            child = (*prefix, name)
            paths.extend(_collect_commands(cmd, child))
    return paths


_ALL_COMMANDS = _collect_commands(repomatic)


@pytest.mark.once
@pytest.mark.parametrize(
    "cmd_path",
    _ALL_COMMANDS,
    ids=[" ".join(p) if p else "repomatic" for p in _ALL_COMMANDS],
)
def test_help_renders(cmd_path: tuple[str, ...]) -> None:
    """Every command and subcommand must render --help without crashing."""
    runner = CliRunner()
    args = ["--no-color", *cmd_path, "--help"]
    result = runner.invoke(repomatic, args, catch_exceptions=False)
    assert result.exit_code == 0, (
        f"repomatic {' '.join(cmd_path)} --help failed "
        f"(exit code {result.exit_code}):\n{result.output}"
    )
