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

"""Read and write the committed CSV files that hold this project's own data.

Every dataset repomatic accrues in a repository is a flat table: a scan
verdict, a binary in a release, a metric reading. CSV is what they are stored
as, and this is the one place that decides how.

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


def write_csv(path: Path, content: str) -> bool:
    """Write *content* to *path*, leaving an unchanged file alone.

    Creates the parent directories when missing. Comparing before writing is
    what keeps a scheduled regeneration a no-op rather than a commit.

    :param path: Path to the CSV file.
    :param content: Rendered CSV, from {func}`render_csv`.
    :return: `True` when the file content changed.
    """
    if path.exists() and path.read_text(encoding="UTF-8") == content:
        logging.debug(f"{path} already up to date.")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="UTF-8")
    return True
