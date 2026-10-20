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

"""Tests for `repomatic.file_ops`: how a file lands on disk."""

from __future__ import annotations

import pytest

from repomatic import __version__
from repomatic.file_ops import (
    GENERATED_BY_PREFIX,
    generated_header,
    normalize_text,
    strip_generated_header,
    write_if_changed,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("papaya", "papaya\n"),
        ("papaya\n", "papaya\n"),
        ("papaya\n\n\n", "papaya\n"),
        ("papaya  \t\n", "papaya\n"),
        ("", "\n"),
    ],
)
def test_normalize_text(text: str, expected: str) -> None:
    """Trailing whitespace collapses to one newline."""
    assert normalize_text(text) == expected


def test_generated_header_names_the_command_and_version() -> None:
    """The first line names the command and the running version."""
    first, second = generated_header("repomatic sync-mailmap").splitlines()
    assert first.startswith(f"# {GENERATED_BY_PREFIX}repomatic sync-mailmap")
    assert f"v{__version__}" in first
    assert second.startswith("# Timestamp: ")


@pytest.mark.parametrize(
    "body",
    [
        "Kiwi <kiwi@example.com>",
        "# A comment of the file.\nKiwi <kiwi@example.com>",
        "",
    ],
)
def test_strip_generated_header_round_trip(body: str) -> None:
    """Stripping the stamp returns the body that followed it."""
    stamped = generated_header("repomatic sync-mailmap") + "\n" + body
    assert strip_generated_header(stamped) == body


def test_strip_generated_header_keeps_an_unstamped_file() -> None:
    """A file without the stamp comes back whole."""
    content = "# Written by hand.\nMelon <melon@example.com>"
    assert strip_generated_header(content) == content


def test_write_if_changed_trusts_previous(tmp_path) -> None:
    """A *previous* equal to the content skips the write, without a read."""
    target = tmp_path / "fruit.txt"
    assert write_if_changed(target, "papaya\n") is True
    target.write_text("melon\n", encoding="UTF-8")
    # The caller vouches for what the file holds, so nothing is written.
    assert write_if_changed(target, "papaya\n", previous="papaya\n") is False
    assert target.read_text(encoding="UTF-8") == "melon\n"
