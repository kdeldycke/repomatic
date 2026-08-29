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
"""Regenerate the committed CLI captures in `docs/assets/`.

Runs as phase 3 of `repomatic update-docs` (the `[tool.repomatic] docs.update-script`
default names this file), so the autofix lane's `update-docs` job refreshes the
captures on every push and ships them in its pull request.

The captures go through the `click-extra screenshot` CLI rather than a
`click:run` `:screenshot:` block, because a Sphinx block writes its image at
*build* time and this lane runs no Sphinx build: the CLI is the only capture
path that lands the SVG in the committed tree the readme links to.

```{note}
The canonical bytes are the ones the Linux `update-docs` runner writes. The
help screen renders the `--config` option's default path through
`click.get_app_dir`, which answers `~/.config/repomatic` on Linux and
`~/Library/Application Support/repomatic` on macOS, so a macOS run of
`repomatic update-docs` rewrites that one line and the next `update-docs`
pull request converges it back. Discard the local flip rather than
committing it.
```
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"
"""Where the committed captures live, beside the other readme illustrations."""

CAPTURES: tuple[tuple[str, str, str], ...] = (
    ("help-dark-screen.svg", "dark", "dark"),
    ("help-light-screen.svg", "light", "light"),
)
"""One `(filename, background, theme)` triple per committed capture.

The readme's `<picture>` element serves the pair on the reader's own color
scheme, so both captures show the same screen: the full `repomatic --help`,
listing every subcommand section.

The capture's CSS class names derive from the output file's stem, so a check
run must compare a capture written under the *same* filename.
"""


def capture(target: Path, background: str, theme: str) -> None:
    """Write one help-screen capture to *target*.

    :param target: The SVG file to write.
    :param background: Terminal chrome the capture is drawn on.
    :param theme: The `--theme` the captured CLI renders with.
    :raises SystemExit: When the capture command fails.
    """
    cmd = [
        "click-extra",
        "screenshot",
        "--output",
        str(target),
        "--background",
        background,
        "--",
        "repomatic",
        "--theme",
        theme,
        "--help",
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode:
        sys.exit(f"Capture failed with exit code {result.returncode}: {cmd}")


def main() -> None:
    """Refresh every capture, or report drift under `--check`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any committed capture is stale.",
    )
    args = parser.parse_args()

    stale: list[str] = []
    for filename, background, theme in CAPTURES:
        committed = ASSETS_DIR / filename
        if not args.check:
            capture(committed, background, theme)
            print(f"Captured {committed}.")
            continue
        # Same filename in a scratch directory, so the stem-derived CSS class
        # names match and the comparison is byte-for-byte.
        with tempfile.TemporaryDirectory() as scratch:
            fresh = Path(scratch) / filename
            capture(fresh, background, theme)
            if not committed.is_file() or committed.read_bytes() != fresh.read_bytes():
                stale.append(filename)

    if stale:
        sys.exit(f"Stale capture(s): {', '.join(stale)}. Run `repomatic update-docs`.")


if __name__ == "__main__":
    main()
