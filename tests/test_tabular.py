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

"""Tests for the shared CSV read, render and write helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from repomatic.tabular import read_csv, render_csv, write_csv

REPO_ROOT = Path(__file__).parent.parent

COMMITTED_STORES = (
    "docs/assets/binaries.csv",
    "docs/assets/metrics.csv",
    "docs/assets/virustotal-scans.csv",
)
"""Every CSV this repository commits, which all go through these helpers."""


def test_render_csv_round_trips(tmp_path):
    """Check what is rendered reads back as the same cells."""
    headers = ("fruit", "colour", "count")
    rows = [("papaya", "orange", 3), ("apricot", "amber", 12)]
    content = render_csv(headers, rows)
    assert content == "fruit,colour,count\npapaya,orange,3\napricot,amber,12\n"

    path = tmp_path / "fruit.csv"
    write_csv(path, content)
    assert read_csv(path) == [
        {"fruit": "papaya", "colour": "orange", "count": "3"},
        {"fruit": "apricot", "colour": "amber", "count": "12"},
    ]


def test_render_csv_quotes_a_cell_carrying_a_separator():
    """Check a comma or a newline inside a cell cannot split a row."""
    content = render_csv(("fruit", "note"), [("papaya", "sweet, ripe")])
    assert content == 'fruit,note\npapaya,"sweet, ripe"\n'
    assert len(content.splitlines()) == 2


def test_render_csv_uses_unix_newlines():
    """Check the output is platform-independent.

    These files are committed, so a `\\r\\n` from a Windows runner would churn
    the whole file against what a Unix runner wrote.
    """
    content = render_csv(("fruit",), [("papaya",), ("apricot",)])
    assert "\r" not in content
    assert content.endswith("\n")


def test_read_csv_tolerates_a_missing_file(tmp_path):
    """Check a first run reads no rows rather than failing."""
    assert read_csv(tmp_path / "absent.csv") == []


def test_read_csv_is_loud_on_a_headerless_file(tmp_path):
    """Check a truncated file raises rather than reading as empty.

    Reading it as empty would let the next write clobber whatever survived.
    """
    path = tmp_path / "broken.csv"
    path.write_text("", encoding="UTF-8")
    with pytest.raises(ValueError, match="no header row"):
        read_csv(path)


def test_write_csv_is_convergent(tmp_path):
    """Check rewriting identical content touches nothing.

    Comparing before writing is what keeps a scheduled regeneration a no-op
    rather than a commit.
    """
    path = tmp_path / "nested" / "fruit.csv"
    content = render_csv(("fruit",), [("papaya",)])
    assert write_csv(path, content) is True
    assert write_csv(path, content) is False
    assert write_csv(path, render_csv(("fruit",), [("apricot",)])) is True


@pytest.mark.parametrize("relative", COMMITTED_STORES)
def test_committed_stores_are_readable(relative):
    """Check every CSV this repository commits parses and carries rows.

    These are written unattended by scheduled and release jobs, so a header
    that drifted or a half-written file would otherwise surface as a broken
    docs page rather than a red test.
    """
    path = REPO_ROOT / relative
    if not path.exists():
        pytest.skip(f"{relative} not generated yet")
    rows = read_csv(path)
    assert rows, f"{relative} exists but holds no row"
    headers = set(rows[0])
    assert all(set(row) == headers for row in rows), "a row has stray columns"
    # Rewriting what was read must be a no-op, which is what proves the
    # committed file is already in this renderer's canonical form.
    columns = list(rows[0])
    rendered = render_csv(columns, [[row[key] for key in columns] for row in rows])
    assert rendered == path.read_text(encoding="UTF-8"), (
        f"{relative} is not in canonical CSV form"
    )
