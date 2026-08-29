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

The captures go through the `click-extra screenshot` and `click-extra snippet`
CLIs (and the recording API) rather than `click:run` `:screenshot:` blocks,
because a Sphinx block writes its image at *build* time and this lane runs no
Sphinx build: these are the only capture paths that land the SVGs in the
committed tree the readme and the docs pages link to.

Every capture scrubs `TERM_PROGRAM` from the environment, so the emoji-width
padding click-extra applies for Apple Terminal never reaches the committed
bytes: whatever terminal runs this script, the capture renders the
standards-conforming layout the Linux runner produces.

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
import codecs
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from click_extra import args_cleanup, format_cli_prompt
from click_extra.color import forced_color
from click_extra.recording import (
    ERASE_IN_LINE,
    SELECT_GRAPHIC_RENDITION,
    ScreenRecorder,
    TerminalScreen,
    quantize,
)
from click_extra.screenshot import CAPTURE_TERMINAL_HINTS, CaptureBackground, render
from extra_platforms import is_unix
from wcwidth import wcswidth

# A pseudo-terminal needs termios, which Windows does not ship: the recording
# below guards on `is_unix` before reaching these.
if is_unix():
    import fcntl
    import pty
    import termios

ASSETS_DIR = Path(__file__).parent / "assets"
"""Where the committed captures live, beside the other readme illustrations."""

HELP_CAPTURES: tuple[tuple[str, str, str], ...] = (
    ("help-dark-screen.svg", "dark", "dark"),
    ("help-light-screen.svg", "light", "light"),
)
"""One `(filename, background, theme)` triple per committed help capture.

The readme's `<picture>` element serves the pair on the reader's own color
scheme, so both captures show the same screen: the full `repomatic --help`,
listing every subcommand section.

The capture's CSS class names derive from the output file's stem, so a check
run must compare a capture written under the *same* filename.
"""

GRID_CAPTURE = "matrix-grid-screen.svg"
"""This repository's own test matrix, pivoted by `show-test-matrix --grid`.

Illustrates the compact grid on `docs/test-matrix.md`. Captured at `auto`
width, so the emoji grid lays out at its own widest row instead of folding
inside an 80-column window.
"""

SNIPPET_CAPTURE = "config-snippet-screen.svg"
"""A `[tool.repomatic]` example highlighted by `click-extra snippet`.

Illustrates `docs/configuration.md`. The source below is written to a scratch
`pyproject.toml`, so Pygments guesses the TOML lexer from the file name.
"""

SNIPPET_SOURCE = """\
[tool.repomatic]
minimum-release-age = "2 weeks"
exclude = [ "subagents", "skills/av-false-positive" ]

[tool.repomatic.manpages]
script = "basket.cli:basket"

[tool.repomatic.labels.content-rules]
"🍒 cherry" = [ "cherry", "/pit(ted)?/i" ]
"""
"""The configuration example {data}`SNIPPET_CAPTURE` draws.

Real keys, neutral values: the component and skill names must exist for the
example to be truthful, while the free values follow the fruit palette.
"""

RECORDING_CAPTURE = "sync-deps-trail-screen.svg"
"""An animated recording of a `sync-deps` dry run, for the readme.

Committed once and never rewritten: the run is timed by the wall clock and
talks to live registries, so no two takes are byte-identical and a
regenerating job would churn forever. Delete the file and run
`repomatic update-docs` to record a fresh take. Same contract as the
`click:run` `:screenshot-record:` Sphinx option.
"""

RECORDING_ARGS = (
    "repomatic",
    "--theme",
    "dark",
    "sync-deps",
    "--dry-run",
)
"""The command the recording runs.

The readme demonstrates this exact invocation right above the animation, so
the picture shows the command the reader was just told to try.
"""

RECORDING_COLUMNS = 120
"""Terminal width the recording runs at.

The closing report tables lay themselves out wider still; past this width the
terminal wraps them, exactly as a real one would.
"""

RECORDING_ROWS = 28
"""Terminal height the recording runs at."""

RECORDING_HOLD = 10.0
"""Seconds the final screen stays up before the animation loops.

The closing report runs several tables long, so the reader gets a proper
pause on the result instead of losing it to the restart. Approximates what
click-extra's unreleased `hold="auto"` computes for this screen (a quarter
second per populated line, so 9.75s for the 39 lines a recent take ended on):
switch to that sentinel when the release carrying it ships, and this constant
goes along with the shim above.
"""

RECORDING_BLANK = 1.0
"""Seconds of empty screen closing the cycle.

Marks the loop boundary: after the long hold above, an instant jump back to
the first frame would read as a glitch rather than a restart.
"""

RECORDING_MIN_FRAMES = 8
"""Fewest frames an acceptable take holds.

A take below this drew no spinner worth animating (an unusually fast run), so
it is retried rather than committed.
"""

RECORDING_TAKES = 3
"""How many takes to attempt before giving up on a clean recording."""


def capture_env() -> dict[str, str]:
    """The environment every capture runs with.

    See the module docstring for why `TERM_PROGRAM` is scrubbed.
    """
    env = dict(os.environ)
    env.pop("TERM_PROGRAM", None)
    return env


def capture_help(target: Path, background: str, theme: str) -> None:
    """Write one help-screen capture to *target*.

    :param target: The SVG file to write.
    :param background: Terminal chrome the capture is drawn on.
    :param theme: The `--theme` the captured CLI renders with.
    :raises SystemExit: When the capture command fails.
    """
    run_capture_tool(
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
    )


def capture_grid(target: Path) -> None:
    """Write the test-matrix grid capture to *target*."""
    run_capture_tool(
        "screenshot",
        "--output",
        str(target),
        "--columns",
        "auto",
        "--",
        "repomatic",
        "--theme",
        "dark",
        "show-test-matrix",
        "--grid",
    )


def capture_snippet(target: Path) -> None:
    """Write the configuration snippet capture to *target*."""
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "pyproject.toml"
        source.write_text(SNIPPET_SOURCE, encoding="UTF-8")
        run_capture_tool(
            "snippet",
            "--output",
            str(target),
            "--title",
            "pyproject.toml",
            str(source),
        )


def run_capture_tool(*args: str) -> None:
    """Run one `click-extra` capture command.

    :param args: The subcommand and its arguments.
    :raises SystemExit: When the command fails.
    """
    cmd = ["click-extra", *args]
    result = subprocess.run(cmd, check=False, env=capture_env())
    if result.returncode:
        sys.exit(f"Capture failed with exit code {result.returncode}: {cmd}")


class DeferredReturnScreen(TerminalScreen):
    """A screen whose carriage return defers its redraw until something lands.

    The released `TerminalScreen` clears the row the moment a `\r` arrives.
    Under a pseudo-terminal every newline travels as `\r\n`, so each
    completed line is erased by its own terminator and the frames keep only
    the line drawn last. Deferring the clear to the next landing text or
    styling keeps a finished line while still letting an animation redraw its
    row in place. Part of the {func}`record_frames` shim, and dropped with it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending_return = False

    def _land(self) -> None:
        """Apply a deferred carriage return, just before something lands."""
        if self._pending_return:
            self.rows[-1] = ""
            self._pending_return = False

    def _control(self, sequence: str) -> None:
        final = sequence[-1]
        if final == SELECT_GRAPHIC_RENDITION:
            self._land()
            self.rows[-1] += sequence
        elif final == ERASE_IN_LINE and not self._column:
            self.rows[-1] = ""
            self._pending_return = False

    def _write(self, text: str) -> None:
        for line_index, line in enumerate(text.split("\n")):
            if line_index:
                # A newline settles any return before it without a redraw.
                self.rows.append("")
                self._column = 0
                self._pending_return = False
            for chunk_index, chunk in enumerate(line.split("\r")):
                if chunk_index:
                    self._column = 0
                    self._pending_return = True
                if not chunk:
                    continue
                self._land()
                self.rows[-1] += chunk
                self._column += max(wcswidth(chunk), 0)


def record_frames(args: tuple[str, ...], columns: int, rows: int) -> tuple:
    """Run *args* under a pseudo-terminal and record the screens it draws.

    A local stand-in for `click_extra.recording.record_command`, differing in
    two behaviors. The pty stream is decoded *incrementally*: a pseudo-terminal
    hands bytes back in kernel-buffer-sized reads, so a multi-byte glyph
    regularly straddles two of them, and the released `record_command` decodes
    each chunk on its own, mangling every straddling glyph into `U+FFFD`. And
    the screens rebuild through {class}`DeferredReturnScreen`, so the `\r\n`
    the pseudo-terminal substitutes for every newline stops erasing the line
    it ends.

    ```{todo}
    Drop this shim, {class}`DeferredReturnScreen` and the whole
    {func}`capture_recording` pipeline once click-extra ships
    `click-extra screenshot --record`: one CLI call then covers the pty
    recording, the prompt line and the auto-scaled hold.
    ```

    :param args: The command line to record.
    :param columns: Width of the terminal it runs in, in characters.
    :param rows: Height of that terminal, in characters.
    :return: The frames the terminal held, in order.
    """
    with forced_color():
        environment = capture_env()
    environment.update(CAPTURE_TERMINAL_HINTS[CaptureBackground.DARK])
    environment["COLUMNS"] = str(columns)
    environment["LINES"] = str(rows)
    environment.pop("TERM_PROGRAM", None)

    recorder = ScreenRecorder()
    # Part of the shim above: the recorder's own screen erases a line the
    # moment its `\r\n` terminator arrives, so every completed row of the
    # trail would vanish from the frames.
    recorder._screen = DeferredReturnScreen()
    decoder = codecs.getincrementaldecoder("UTF-8")(errors="replace")
    parent, child = pty.openpty()
    fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    process = subprocess.Popen(
        args_cleanup(args),
        stdin=child,
        stdout=child,
        stderr=child,
        env=environment,
        close_fds=True,
    )
    os.close(child)
    try:
        while True:
            readable, _, _ = select.select([parent], [], [], 0.02)
            if not readable:
                if process.poll() is not None:
                    break
                continue
            try:
                written = os.read(parent, 65536)
            except OSError:
                # The child closed its end, which a pseudo-terminal reports
                # as an error rather than as the end of a file.
                break
            if not written:
                break
            recorder.write(decoder.decode(written))
    finally:
        tail = decoder.decode(b"", final=True)
        if tail:
            recorder.write(tail)
        os.close(parent)
        if process.poll() is None:
            process.terminate()
        process.wait()
    return recorder.frames(end=time.monotonic())


def capture_recording(target: Path) -> None:
    """Record {data}`RECORDING_ARGS` under a pseudo-terminal and write *target*.

    Retries a take whose frames carry a `U+FFFD` replacement character (a
    child that wrote genuinely malformed bytes) and a take too short to
    animate (an unusually fast run that drew no spinner worth keeping).

    :param target: The animated SVG file to write.
    :raises SystemExit: When no clean take comes out of
        {data}`RECORDING_TAKES` attempts.
    """
    if not is_unix():
        print("Recording needs a pseudo-terminal: skipped on this platform.")
        return

    # The invocation is drawn above the output as a shell prompt on every
    # frame, exactly as the `click-extra screenshot` stills draw theirs, so
    # the animation shows what to type to reproduce it.
    with forced_color():
        prompt_line = format_cli_prompt(RECORDING_ARGS)

    for take in range(1, RECORDING_TAKES + 1):
        frames = quantize(
            record_frames(
                RECORDING_ARGS,
                columns=RECORDING_COLUMNS,
                rows=RECORDING_ROWS,
            )
        )
        texts = tuple(f"{prompt_line}\n{frame.text}" for frame in frames)
        degraded = any("�" in text or "rate limit exceeded" in text for text in texts)
        if len(frames) < RECORDING_MIN_FRAMES or degraded:
            print(f"Take {take} rejected (short, mangled or degraded), retrying.")
            continue
        svg = render(
            texts[-1],
            frames=texts,
            interval=tuple(frame.duration for frame in frames),
            hold=RECORDING_HOLD,
            blank=RECORDING_BLANK,
            columns=RECORDING_COLUMNS,
            unique_id=target.stem,
        )
        target.write_text(svg, encoding="UTF-8")
        return
    sys.exit(f"No clean recording after {RECORDING_TAKES} takes.")


def main() -> None:
    """Refresh every capture, or report drift under `--check`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any committed capture is stale.",
    )
    args = parser.parse_args()

    deterministic: list[tuple[str, object]] = [
        *(
            (filename, lambda t, b=background, th=theme: capture_help(t, b, th))
            for filename, background, theme in HELP_CAPTURES
        ),
        (GRID_CAPTURE, capture_grid),
        (SNIPPET_CAPTURE, capture_snippet),
    ]

    stale: list[str] = []
    for filename, capture in deterministic:
        committed = ASSETS_DIR / filename
        if not args.check:
            capture(committed)  # type: ignore[operator]
            print(f"Captured {committed}.")
            continue
        # Same filename in a scratch directory, so the stem-derived CSS class
        # names match and the comparison is byte-for-byte.
        with tempfile.TemporaryDirectory() as scratch:
            fresh = Path(scratch) / filename
            capture(fresh)  # type: ignore[operator]
            if not committed.is_file() or committed.read_bytes() != fresh.read_bytes():
                stale.append(filename)

    # The recording is committed once: only its absence counts as drift.
    recording = ASSETS_DIR / RECORDING_CAPTURE
    if not recording.is_file():
        if args.check:
            stale.append(RECORDING_CAPTURE)
        else:
            capture_recording(recording)
            print(f"Recorded {recording}.")

    if stale:
        sys.exit(f"Stale capture(s): {', '.join(stale)}. Run `repomatic update-docs`.")


if __name__ == "__main__":
    main()
