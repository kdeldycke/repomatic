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

"""Render and persist the flat tables repomatic produces.

Two surfaces, one module, because both are the same shape of data: the CSV
files a repository commits (a scan verdict, a binary in a release, a metric
reading) and the Markdown tables its reports embed (a PR body's diff table, a
step summary's tally). {func}`render_markdown_table` is the one Markdown table
renderer, so every report agrees on the cell and separator spelling; the CSV
trio below decides how the committed datasets are stored.

```{note}
CSV over JSON for these, on four counts a flat table makes decisive:

- **Diff churn.** A record is one line, not seven. These files are sorted, so a
  scheduled append lands mid-file rather than at the end: ten new readings cost
  ten inserted lines instead of seventy.
- **Size.** Roughly half, and the gap widens as a history accrues.
- **Rendering.** MyST's `csv-table` directive reads one directly, and GitHub
  serves a committed CSV through its own searchable grid viewer where JSON is
  raw text.
- **No formatter contention.** Nothing in the autofix lane touches CSV, where
  a committed JSON file has to be serialized in Biome's exact style or
  `format-json` rewrites it right back.

JSON earns its place where a record nests. None of these do.
```
"""

from __future__ import annotations

import csv
import io
import logging

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path


def render_csv(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """Render a header row and its data rows as CSV text.

    Newlines are `\\n` on every platform, since the output is committed and a
    platform-dependent line ending would make the file churn between a
    Windows and a Unix runner.

    :param headers: Column names, in order.
    :param rows: One sequence of cells per row, in the same order.
    :return: The complete CSV document, newline-terminated.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


_ALIGN_MARKERS = {"": "---", "center": ":-:", "left": ":--", "right": "--:"}
"""GFM delimiter-row cell per alignment name, empty for the parser default."""


def render_markdown_table(
    headers: Sequence[object],
    rows: Iterable[Sequence[object]],
    align: Sequence[str] = (),
) -> str:
    """Render a GitHub-flavored Markdown table.

    Cells are used as given: a caller wanting a code span, a link or an emoji
    renders it into the cell first. Nothing is escaped, matching what every
    report renderer did by hand before this existed: none of them ever feeds a
    cell carrying a `|`.

    :param headers: Column titles, in order.
    :param rows: One sequence of cells per row, in the same order.
    :param align: Per-column alignment, `left`, `right` or `center`; an empty
        entry (or a list shorter than *headers*) leaves that column on the
        parser default. Alignment only changes how a *renderer* justifies the
        column, so it is worth declaring where it carries meaning, like a
        numeric column read against its neighbours.
    :return: The table's lines joined with newlines, no trailing newline.
    :raises KeyError: On an alignment name outside the vocabulary.
    """
    markers = [
        _ALIGN_MARKERS[align[index] if index < len(align) else ""]
        for index in range(len(headers))
    ]
    lines = [
        "| " + " | ".join(str(header) for header in headers) + " |",
        "| " + " | ".join(markers) + " |",
    ]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read a committed CSV into one mapping per row.

    Every cell comes back as a string: CSV carries no types, so a caller
    wanting a number coerces it. A missing file reads as no rows, which is
    what a first run sees.

    :param path: Path to the CSV file.
    :return: One mapping per data row, keyed by column name.
    :raises ValueError: When the file exists but carries no header row. Loud on
        purpose: a truncated or half-written file must never be silently
        treated as empty and clobbered by the next {func}`write_csv`.
    """
    if not path.exists():
        return []
    with path.open(encoding="UTF-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            msg = f"Malformed CSV {path}: no header row."
            raise ValueError(msg)
        return [dict(row) for row in reader]


def write_if_changed(path: Path, content: str) -> bool:
    """Write *content* to *path*, leaving an already-matching file alone.

    Creates the parent directories when missing. Comparing before writing is
    what every generator in the package leans on: one that rewrote its output
    unconditionally would turn each scheduled run into a commit, and a sync
    job that opens a pull request would open one forever.

    Format-neutral despite sitting beside the CSV helpers, because what it
    encodes is the write rather than the bytes. The SVG charts in
    {mod}`repomatic.metric_chart` route through it too.

    :param path: File to write.
    :param content: The full text the file should hold.
    :return: `True` when the file was created or its content changed.
    """
    if path.exists() and path.read_text(encoding="UTF-8") == content:
        logging.debug(f"{path} already up to date.")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="UTF-8")
    return True


def write_csv(path: Path, content: str) -> bool:
    """Write rendered CSV to *path*, leaving an unchanged file alone.

    :param path: Path to the CSV file.
    :param content: Rendered CSV, from {func}`render_csv`.
    :return: `True` when the file content changed.
    """
    return write_if_changed(path, content)
