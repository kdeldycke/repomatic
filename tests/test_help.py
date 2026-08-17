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

"""Conformance of every command's `--help`: that it is exercised, and readable.

The `--help` invocations themselves live as `[[cases]]` entries in
`tests/cli-test-suite.toml`, executed by `click-extra test-suite` in CI and
against the compiled binaries during releases. A static roster drifts as
commands are added or removed, so the first test compares it against the live
command tree and fails naming the missing or orphaned entries.

Exercising a command's help says nothing about how it reads, which the second
test covers: it walks the same tree and rejects a docstring whose example
blocks Click will rewrap into prose.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import click
import pytest
import tomlrt

from repomatic.cli import repomatic

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

SUITE_PATH = Path(__file__).parent / "cli-test-suite.toml"

NO_REWRAP = "\b"
"""Click's escape suppressing the rewrapping of one paragraph.

It only holds for the paragraph it opens, and only when it is that paragraph's
*first* line: `click.formatting.wrap_text` tests `buf[0]` and nothing else.
"""


def _collect_commands(
    group: click.Group | click.Command,
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], click.Command]]:
    """Recursively collect every command path from a Click group, with its command."""
    found: list[tuple[tuple[str, ...], click.Command]] = [(prefix, group)]
    if isinstance(group, click.Group):
        for name in sorted(group.list_commands(click.Context(group))):
            cmd = group.get_command(click.Context(group), name)
            if cmd is None:
                continue
            found.extend(_collect_commands(cmd, (*prefix, name)))
    return found


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
    tree = {path for path, _cmd in _collect_commands(repomatic)}
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


def _paragraphs(text: str) -> Iterator[list[str]]:
    """Split a docstring into its blank-line-separated blocks of lines."""
    block: list[str] = []
    for line in text.splitlines():
        if line.strip():
            block.append(line)
        elif block:
            yield block
            block = []
    if block:
        yield block


def _rewrapped_blocks(help_text: str) -> list[str]:
    """Indented paragraphs Click will rewrap, for want of their own marker.

    An indented block is there to be read as written: an example invocation,
    a sample of output. Rewrapping folds it into prose, so a second block
    under one `NO_REWRAP` marker loses its line breaks and its command lands
    appended to the comment above it.

    :param help_text: A command's raw help text.
    :return: The first line of each unguarded block, for the failure message.
    """
    return [
        body[0].strip()
        for block in _paragraphs(inspect.cleandoc(help_text))
        if block[0].strip() != NO_REWRAP
        and (body := [line for line in block if line.strip() != NO_REWRAP])
        and all(line.startswith("    ") for line in body)
    ]


@pytest.mark.once
def test_every_preformatted_block_carries_its_own_marker() -> None:
    """Every indented block in a `--help` text must open with its own marker.

    Writing one marker above a run of example blocks reads as if it covered
    them all, and renders the first correctly, which is what lets the rest
    ship collapsed. Both offenders this caught had exactly that shape.
    """
    offenders = {
        " ".join(path) or "(root)": blocks
        for path, cmd in _collect_commands(repomatic)
        if (blocks := _rewrapped_blocks(cmd.help or ""))
    }
    assert not offenders, "Indented --help blocks Click will rewrap: " + "; ".join(
        f"{name} ({', '.join(blocks)})" for name, blocks in sorted(offenders.items())
    )
