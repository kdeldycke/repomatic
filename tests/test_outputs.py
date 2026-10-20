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

"""Tests for :mod:`repomatic.github.outputs` step outputs and annotations."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from repomatic.github.outputs import (
    MAX_STEP_OUTPUT_BYTES,
    AnnotationLevel,
    emit_report,
    format_file_output,
    format_multiline_output,
    read_file_output,
    report_finding,
    trim_to_byte_budget,
    write_output_file,
    write_text_output,
)

# -- AnnotationLevel ----------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "glyph"),
    (
        (AnnotationLevel.ERROR, "✗"),
        (AnnotationLevel.WARNING, "⚠"),
        (AnnotationLevel.NOTICE, "ℹ"),
    ),
)
def test_annotation_level_glyph(level, glyph):
    """Each level opens its terminal line with its own symbol."""
    assert level.glyph == glyph


@pytest.mark.parametrize("level", AnnotationLevel)
def test_report_finding_annotates_then_prints(level, capsys):
    """The annotation and the terminal line always agree on the level."""
    report_finding(level, "Mango bruised.")
    assert capsys.readouterr().out == (
        f"::{level.value}::Mango bruised.\n{level.glyph} Mango bruised.\n"
    )


# -- Step output size guard ---------------------------------------------------


def _output_value(formatted):
    """Recover the heredoc payload the runner would assign to the variable."""
    return "\n".join(formatted.split("\n")[1:-1])


@pytest.mark.parametrize(
    "value",
    (
        "",
        "mango",
        "| Fruit | Crate |\n| :-- | :-- |\n| mango | 10 |",
        "kiwi\n" * 100,
    ),
)
def test_format_multiline_output_keeps_small_values_verbatim(value):
    """Anything under the ceiling reaches the consuming step untouched."""
    assert _output_value(format_multiline_output("harvest", value)) == value


@pytest.mark.parametrize(
    ("filler", "label"),
    (
        ("mango ripens in July.", "ascii"),
        ("柑橘は七月に熟す。", "three-byte"),
        ("🍊 ripens in July.", "astral"),
    ),
)
def test_format_multiline_output_trims_oversized_values(filler, label):
    """An oversized value is cut down to fit, whatever its encoding costs."""
    oversized = "\n".join([filler * 20] * 4000)
    assert len(oversized.encode("UTF-8")) > MAX_STEP_OUTPUT_BYTES

    trimmed = _output_value(format_multiline_output("harvest", oversized))
    assert len(trimmed.encode("UTF-8")) <= MAX_STEP_OUTPUT_BYTES
    # Still decodable: a mid-character byte slice would not survive this.
    assert trimmed.encode("UTF-8").decode("UTF-8") == trimmed
    # Leading content survives verbatim, cut on a line boundary.
    assert oversized.startswith(trimmed)
    assert all(line in oversized.splitlines() for line in trimmed.splitlines())


def test_format_multiline_output_leaves_room_for_the_variable_name():
    """The trimmed value plus a `NAME=` prefix stays under `MAX_ARG_STRLEN`.

    The kernel counts the whole `NAME=value` string against its per-string
    cap, so a value sized to the cap exactly would still fail to exec.
    """
    max_arg_strlen = 32 * 4096
    trimmed = _output_value(format_multiline_output("harvest", "mango\n" * 40000))
    entry = len("REPOMATIC_DIFF_TABLE=") + len(trimmed.encode("UTF-8"))
    assert entry <= max_arg_strlen


def test_trim_to_byte_budget_cuts_on_line_boundaries():
    """A budget landing mid-line drops that line rather than splitting it."""
    text = "mango\nkiwi\npapaya"
    # Room for "mango\nkiwi\n" and one byte of "papaya".
    assert trim_to_byte_budget(text, 12) == "mango\nkiwi"


def test_trim_to_byte_budget_returns_empty_when_nothing_fits():
    """A budget too small for even the first line yields nothing."""
    assert trim_to_byte_budget("watermelon\nkiwi", 3) == ""


# -- File-backed step outputs -------------------------------------------------


def test_write_output_file_lands_in_the_runner_temp_dir(tmp_path, monkeypatch):
    """The runner's own temp dir holds the spill when it exports one."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    spilled = write_output_file("harvest", "mango")
    assert spilled.parent == tmp_path
    assert spilled.name.startswith("repomatic-harvest-")
    assert spilled.read_text(encoding="UTF-8") == "mango"


def test_format_file_output_names_the_output_after_the_file(tmp_path, monkeypatch):
    """The step output carries a path under a `_file` name, not the content."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    name, _, path = format_file_output("harvest", "mango\nkiwi").partition("=")
    assert name == "harvest_file"
    assert Path(path).read_text(encoding="UTF-8") == "mango\nkiwi"


def test_format_file_output_keeps_oversized_values_whole(tmp_path, monkeypatch):
    """A report past the environment's ceiling reaches the consumer intact."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    oversized = "mango ripens in July.\n" * 20000
    assert len(oversized.encode("UTF-8")) > MAX_STEP_OUTPUT_BYTES

    line = format_file_output("harvest", oversized)
    assert len(line.encode("UTF-8")) < MAX_STEP_OUTPUT_BYTES
    assert Path(line.partition("=")[2]).read_text(encoding="UTF-8") == oversized


def test_read_file_output_reads_the_file_the_variable_names(tmp_path, monkeypatch):
    """The `_FILE` variable names the file whose content is the value."""
    spilled = tmp_path / "harvest.md"
    spilled.write_text("mango", encoding="UTF-8")
    monkeypatch.setenv("HARVEST_FILE", str(spilled))
    assert read_file_output("HARVEST") == "mango"


def test_read_file_output_ignores_an_inline_value(monkeypatch):
    """Only the path counts: a value passed inline is not read."""
    monkeypatch.delenv("HARVEST_FILE", raising=False)
    monkeypatch.setenv("HARVEST", "kiwi")
    assert read_file_output("HARVEST") == ""


def test_file_output_round_trips(tmp_path, monkeypatch):
    """What the producer spills is what the consumer reads back."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    report = "| Fruit | Crate |\n| :-- | :-- |\n| mango | 10 |"
    _, _, path = format_file_output("harvest", report).partition("=")
    monkeypatch.setenv("HARVEST_FILE", path)
    assert read_file_output("HARVEST") == report


# -- Report writes ------------------------------------------------------------


def test_emit_report_writes_scalar_outputs_beside_the_path(tmp_path, monkeypatch):
    """Every output of the step lands in the one write the output file gets."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    output = tmp_path / "github-output"
    emit_report(
        "Ten crates of mango.",
        output,
        "github-actions",
        key="harvest",
        outputs={"season": "summer", "orchard": "Seville"},
    )
    lines = output.read_text(encoding="UTF-8").splitlines()
    assert lines[0].startswith("harvest_file=")
    assert Path(lines[0].partition("=")[2]).read_text(encoding="UTF-8") == (
        "Ten crates of mango."
    )
    assert lines[1:] == ["season=summer", "orchard=Seville"]


def test_emit_report_keeps_scalar_outputs_out_of_markdown(tmp_path):
    """A markdown report is the report alone."""
    output = tmp_path / "report.md"
    emit_report(
        "Ten crates of mango.", output, "markdown", outputs={"season": "summer"}
    )
    assert output.read_text(encoding="UTF-8") == "Ten crates of mango.\n"


@pytest.mark.parametrize(("body", "output"), (("", "report.md"), ("Mango.", None)))
def test_emit_report_writes_nothing_without_a_body_or_a_path(tmp_path, body, output):
    """An empty report and a missing `--output` both leave the disk alone."""
    target = tmp_path / "reports"
    target.mkdir()
    emit_report(body, target / output if output else None, "markdown")
    assert list(target.iterdir()) == []


def test_write_text_output_closes_the_file_it_opens(tmp_path, monkeypatch):
    """The stream opened for a path is closed once written, flushing it."""
    opened = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        stream = real_open(self, *args, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", spy)
    output = tmp_path / "report.md"
    write_text_output("Ten crates of mango.", output)

    assert [stream.closed for stream in opened] == [True]
    assert output.read_text(encoding="UTF-8") == "Ten crates of mango.\n"


def test_write_text_output_leaves_stdout_open(capsys):
    """Writing to `-` never closes the live standard output."""
    write_text_output("Ten crates of mango.", Path("-"))
    assert not sys.stdout.closed
    assert capsys.readouterr().out == "Ten crates of mango.\n"
