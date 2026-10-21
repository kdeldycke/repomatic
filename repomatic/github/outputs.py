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

"""What a command hands back to GitHub Actions: step outputs and annotations.

A step output travels in `$GITHUB_OUTPUT`, inline when its size is bounded and
as a file path when it is a report. An annotation is a workflow command printed
to the log. Both have size and format rules that this module applies in one
place.
"""

from __future__ import annotations

import logging
import os
import sys
from enum import Enum
from pathlib import Path
from random import randint
from tempfile import mkstemp

from click_extra import echo, prep_path

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


MAX_STEP_OUTPUT_BYTES = 32 * 4096 - 1024
"""Ceiling for a single `$GITHUB_OUTPUT` value, in UTF-8 bytes.

A step output only exists to be read by a later step, and the two ways of
reading one both land it in the consumer's environment: `env:` mapping a
`steps.*.outputs.*` expression, and action inputs, which the runner exports as
`INPUT_*`. Linux caps a single `argv`/`envp` string at `MAX_ARG_STRLEN`, 32
pages, so a value past that makes the runner's `execve()` of `/usr/bin/bash`
fail with `E2BIG` before the step's own command exists:

```{code-block} text
##[error]An error occurred trying to start process '/usr/bin/bash' with
working directory '/home/runner/work/orchard/orchard'. Argument list too long
```

The 1 KiB reserve covers the `NAME=` prefix the kernel counts as part of the
same string, well beyond the longest name in use.

This ceiling only binds a value the environment has to carry. A report that
grows without bound belongs in a file instead: see {func}`format_file_output`,
which hands the consumer a path and leaves the content untrimmed.

```{caution}
This is a transport limit, counted in bytes, and is deliberately looser than
{data}`repomatic.github.pr_body.GITHUB_BODY_MAX_CHARS`, which is a content
limit counted in UTF-16 code units. A value can clear this one and still be
trimmed later by {func}`repomatic.github.pr_body.build_pr_body`, which is the
layer that leaves the reader a truncation notice.
```
"""


class AnnotationLevel(Enum):
    """Annotation levels for GitHub Actions workflow commands.

    Mirrors the three levels GitHub supports, even where the codebase only
    emits a subset.
    """

    ERROR = "error"
    WARNING = "warning"
    NOTICE = "notice"

    @property
    def glyph(self) -> str:
        """The symbol that opens a terminal line reporting at this level.

        `✗` for an error and `⚠` for a warning. A notice reads `ℹ`, the symbol
        a skipped check prints too: neither one asks the reader to act.
        """
        return _ANNOTATION_GLYPHS[self]


_ANNOTATION_GLYPHS: dict[AnnotationLevel, str] = {
    AnnotationLevel.ERROR: "\u2717",
    AnnotationLevel.WARNING: "\u26a0",
    AnnotationLevel.NOTICE: "\u2139",
}
"""Terminal symbol per {class}`AnnotationLevel`, read by its `glyph` property."""


def generate_delimiter() -> str:
    """Generate a unique delimiter for GitHub Actions multiline output.

    GitHub Actions requires a unique delimiter to encode multiline values in
    `$GITHUB_OUTPUT`. This function generates a random delimiter that is
    extremely unlikely to appear in the output content.

    The delimiter format is `GHA_DELIMITER_NNNNNNNNN` where N is a digit,
    producing a 9-digit random suffix.

    :return: A unique delimiter string.

    ```{seealso}
    https://github.com/orgs/community/discussions/26288#discussioncomment-3876281
    ```
    """
    return f"GHA_DELIMITER_{randint(10**8, (10**9) - 1)}"


def trim_to_budget(text: str, budget: int, measure: Callable[[str], int]) -> str:
    """Keep the leading whole lines of *text* that fit in *budget*.

    Cutting on line boundaries keeps the trimmed markdown rendering: a table
    missing rows still renders, one cut mid-row does not.

    The one trimming loop behind both of GitHub's size ceilings, which count
    in different units: *measure* prices a line in whatever unit the caller's
    budget is denominated in (UTF-8 bytes for a step output, UTF-16 code units
    for a PR or issue body).

    :param text: Content to trim.
    :param budget: Available room, in *measure*'s unit.
    :param measure: Returns the size of one line in that unit.
    :return: The kept lines, right-stripped; empty when nothing fits.
    """
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        used += measure(line) + 1  # The joining newline.
        if used > budget:
            break
        kept.append(line)
    return "\n".join(kept).rstrip()


def _utf8_len(text: str) -> int:
    """Length of *text* in UTF-8 bytes, the unit `$GITHUB_OUTPUT` is capped in."""
    return len(text.encode("UTF-8"))


def trim_to_byte_budget(text: str, budget: int) -> str:
    """Keep the leading whole lines of *text* that fit in *budget* UTF-8 bytes.

    {func}`trim_to_budget` in the step-output unit. Trimming whole lines also
    keeps the result valid UTF-8, which slicing a byte string cannot promise.

    :param text: Content to trim.
    :param budget: Available room, in UTF-8 bytes.
    :return: The kept lines, right-stripped; empty when nothing fits.
    """
    return trim_to_budget(text, budget, _utf8_len)


def format_multiline_output(name: str, value: str) -> str:
    """Format a multiline value for GitHub Actions output.

    Produces output in the heredoc format required by `$GITHUB_OUTPUT`:

    ```{code-block} text

    name<<GHA_DELIMITER_NNNNNNNNN
    value line 1
    value line 2
    GHA_DELIMITER_NNNNNNNNN
    ```

    Values over {data}`MAX_STEP_OUTPUT_BYTES` are trimmed to fit, so a step
    reading this output cannot be killed by `E2BIG` before it starts.

    :param name: The output variable name.
    :param value: The multiline value.
    :return: Formatted string for `$GITHUB_OUTPUT`.
    """
    size = _utf8_len(value)
    if size > MAX_STEP_OUTPUT_BYTES:
        logging.warning(
            f"Step output {name!r} is {size} bytes, over the "
            f"{MAX_STEP_OUTPUT_BYTES}-byte limit a consuming step can receive: "
            "trimming to fit."
        )
        value = trim_to_byte_budget(value, MAX_STEP_OUTPUT_BYTES)
    delimiter = generate_delimiter()
    return f"{name}<<{delimiter}\n{value}\n{delimiter}"


def write_output_file(name: str, value: str) -> Path:
    """Write a step output's value to a file, and return that file's path.

    The file lands in `RUNNER_TEMP` where the runner exports it, which is
    per-job and cleaned up by the runner itself, and in the system temporary
    directory otherwise. Either is shared by every step of a job, which is what
    makes the handoff work: producer and consumer are separate processes on one
    filesystem.

    :param name: The step output name, which the filename carries so a spilled
        report is recognisable in a temporary directory.
    :param value: The content to write.
    :return: Path of the file holding *value*.
    """
    handle, path = mkstemp(
        prefix=f"repomatic-{name}-",
        suffix=".md",
        dir=os.getenv("RUNNER_TEMP") or None,
    )
    os.close(handle)
    spilled = Path(path)
    spilled.write_text(value, encoding="UTF-8")
    return spilled


def format_file_output(name: str, value: str) -> str:
    """Format a value as a file-backed output, for a consumer to read back.

    Writes *value* out with {func}`write_output_file` and names the step output
    `<name>_file`, so the consuming step receives a path instead of content:

    ```{code-block} text
    harvest_file=/home/runner/work/_temp/repomatic-harvest-8m1t0p.md
    ```

    This is the escape hatch from {data}`MAX_STEP_OUTPUT_BYTES`. A report grows
    with the repository it describes and has no ceiling of its own: the release
    notes of a dependency sweep can reach twice what the environment carries,
    and trimming to fit loses content. A path keeps the environment holding a
    hundred-odd bytes whatever the report weighs, and leaves any trimming to
    {func}`repomatic.github.pr_body.build_pr_body`, which is the layer that
    knows GitHub's own body limit and marks the cut for the reader.

    :param name: The output variable name, before the `_file` suffix.
    :param value: The content to hand over.
    :return: Formatted string for `$GITHUB_OUTPUT`.
    """
    return f"{name}_file={write_output_file(name, value)}"


def read_file_output(name: str) -> str:
    """Read the content of a file-backed value, from its `<NAME>_FILE` path.

    The consuming half of {func}`format_file_output`: the workflow maps the
    `<name>_file` step output to the `<NAME>_FILE` variable of the step that
    reads it.

    :param name: The environment variable name, before the `_FILE` suffix.
    :return: The content of the file, empty when the variable is not set.
    """
    path = os.getenv(f"{name}_FILE")
    if not path:
        return ""
    return Path(path).read_text(encoding="UTF-8")


def write_text_output(content: str, output: Path) -> None:
    """Write *content* to an `--output` path, then close what was opened.

    For `-`, `prep_path` returns the live `sys.stdout` when stdout has no file
    descriptor, as under a test runner. Only a stream it opened is closed:
    closing the live one would end every later write of the run.

    :param content: The text to write. A trailing newline is added.
    :param output: The destination path, or `-` for stdout.
    """
    stream = prep_path(output)
    try:
        echo(content, file=stream)
    finally:
        if stream is not sys.stdout:
            stream.close()


def emit_report(
    body: str,
    output: Path | None,
    output_format: str,
    key: str = "diff_table",
    outputs: Mapping[str, str] | None = None,
) -> None:
    """Write a markdown report to `--output`, optionally as a step output.

    The shared tail of every report-producing command: nothing is written
    when no output path is set or the body is empty; with
    `--output-format github-actions` the body is spilled to a file and the step
    output named `<key>_file` carries its path, for `$GITHUB_OUTPUT`
    consumption.

    A report is the one value here with no ceiling of its own, so it is the one
    that must not travel inline: see {func}`format_file_output`.

    :param body: The markdown report.
    :param output: The `--output` path (`None` to skip, `-` for stdout).
    :param output_format: `markdown` or `github-actions`.
    :param key: The step output variable name, before the `_file` suffix, for
        the `github-actions` format.
    :param outputs: Single-line step outputs written beside the report's path,
        for the `github-actions` format only. `prep_path` truncates the file it
        opens, so every output of a step has to go through this one write.
    """
    if output is None or not body:
        return
    if output_format == "github-actions":
        content = "\n".join((
            format_file_output(key, body),
            *(f"{name}={value}" for name, value in (outputs or {}).items()),
        ))
    else:
        content = body
    write_text_output(content, output)


def emit_annotation(level: AnnotationLevel, message: str) -> None:
    """Emit a GitHub Actions workflow annotation.

    Prints a workflow command that creates an annotation visible in the GitHub
    Actions UI and PR checks.

    :param level: The annotation level.
    :param message: The annotation message.

    ```{seealso}
    https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#setting-an-error-message
    ```
    """
    print(f"::{level.value}::{message}")


def add_problem_matcher(path: Path) -> None:
    """Register the problem matcher at *path* with the GitHub Actions runner.

    The runner reads the file when it processes the command, so the file must
    outlast the process that prints it. The command is flushed at once: a tool
    started next writes its findings to the same stream, and the runner must
    read the command before the first of them.

    ```{seealso}
    https://github.com/actions/toolkit/blob/main/docs/problem-matchers.md
    ```
    """
    print(f"::add-matcher::{path}", flush=True)


def remove_problem_matcher(owner: str) -> None:
    """Unregister the problem matcher of *owner* from the GitHub Actions runner."""
    print(f"::remove-matcher owner={owner}::", flush=True)


def report_finding(level: AnnotationLevel, message: str) -> None:
    """Report one finding in the log and as a workflow annotation.

    Emits the annotation first, then prints the line the terminal reader sees,
    opened by the {attr}`AnnotationLevel.glyph` of *level*. Every lint that
    reports a failure goes through here, so the symbol and the annotation level
    of a finding always agree.

    :param level: Severity of the finding.
    :param message: The finding, one line.
    """
    emit_annotation(level, message)
    echo(f"{level.glyph} {message}")
