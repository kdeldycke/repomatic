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

"""Keeps the CLI test suite's per-command `--help` roster in sync.

The `--help` invocations themselves live as `[[cases]]` entries in
`tests/cli-test-suite.toml`, executed by `click-extra test-suite` in CI and
against the compiled binaries during releases. A static roster drifts as
commands are added or removed, so this conformance test compares it against
the live command tree and fails naming the missing or orphaned entries.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
import tomlrt

from repomatic.cli import repomatic

SUITE_PATH = Path(__file__).parent / "cli-test-suite.toml"


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


def _suite_help_paths() -> set[tuple[str, ...]]:
    """Extract the command path of each `--help` case in the test suite.

    A `--help` case is one whose `cli_parameters` end with `--help`; the
    words before it are the command path (empty for the root CLI).
    """
    suite = tomlrt.loads(SUITE_PATH.read_text(encoding="UTF-8"))
    paths: set[tuple[str, ...]] = set()
    for case in suite["cases"]:
        params = case["cli_parameters"]
        if isinstance(params, str):
            params = params.split()
        if params and params[-1] == "--help":
            paths.add(tuple(params[:-1]))
    return paths


@pytest.mark.once
def test_suite_covers_all_commands() -> None:
    """Every command and subcommand must have a `--help` case in the suite."""
    tree = set(_collect_commands(repomatic))
    suite_paths = _suite_help_paths()
    missing = sorted(tree - suite_paths)
    orphans = sorted(suite_paths - tree)
    assert not missing, (
        "Commands without a --help case in tests/cli-test-suite.toml: "
        + ", ".join(" ".join(path) or "(root)" for path in missing)
    )
    assert not orphans, (
        "--help cases in tests/cli-test-suite.toml without a live command: "
        + ", ".join(" ".join(path) for path in orphans)
    )
