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

"""Runner timings aggregate honestly, and sort as durations rather than strings."""

from __future__ import annotations

import pytest

from repomatic.github.job_timings import (
    UNATTRIBUTED,
    JobTiming,
    _duration,
    format_duration,
    match_runner,
    render_markdown,
    summarize,
)


@pytest.mark.parametrize(
    ("job_name", "expected"),
    [
        ("⁉️ ubuntu-26.04-arm / py3.15-dev", "ubuntu-26.04-arm"),
        # The bare image must not shadow its longer sibling: a prefix scan in
        # registry order reports `ubuntu-26.04` for the Arm job.
        ("✅ ubuntu-26.04 / py3.10", "ubuntu-26.04"),
        ("release / ⁉️ windows-11-arm, abc1234 build", "windows-11-arm"),
        ("📦 Package install", UNATTRIBUTED),
    ],
)
def test_match_runner(job_name: str, expected: str) -> None:
    """A job is attributed to the image its name carries, or to nothing."""
    assert match_runner(job_name) == expected


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (
            {
                "startedAt": "2026-08-15T10:00:00Z",
                "completedAt": "2026-08-15T10:02:30Z",
            },
            150.0,
        ),
        # Cancelled in the queue: no start, so no duration to read.
        ({"startedAt": "", "completedAt": "2026-08-15T10:02:30Z"}, None),
        # Still running: no end.
        ({"startedAt": "2026-08-15T10:00:00Z", "completedAt": ""}, None),
        # Unparsable timestamps are skipped rather than guessed at.
        ({"startedAt": "yesterday", "completedAt": "today"}, None),
    ],
)
def test_duration_skips_what_it_cannot_measure(job: dict, expected: float) -> None:
    """A job with no usable pair is skipped, never counted as zero.

    Counting it as zero would drag a median toward a runner's queue behaviour
    instead of its speed, which is the opposite of what this measures.
    """
    assert _duration(job) == expected


def test_format_duration_sorts_chronologically() -> None:
    """Rendered durations sort as strings in the order they sort as durations.

    `--sort-by` orders the rendered table lexicographically. Unpadded, `21s`
    lands between `1m37s` and `2m03s` and the default view reads as though a
    21-second job were slower than a 97-second one.
    """
    seconds = [21, 60, 97, 123, 130, 3599]
    rendered = [format_duration(value) for value in seconds]
    assert rendered == sorted(rendered), f"string order is not time order: {rendered}"
    assert rendered[0] == "00m21s"


def test_summarize_puts_the_slowest_median_first() -> None:
    """Aggregation answers "which image is holding the matrix up" at the top."""
    timings = [
        JobTiming("a", "fast-image", 10.0),
        JobTiming("b", "fast-image", 20.0),
        JobTiming("c", "slow-image", 100.0),
        JobTiming("d", "slow-image", 300.0),
    ]
    reports = summarize(timings)
    assert [report.runner for report in reports] == ["slow-image", "fast-image"]
    assert reports[0].median_seconds == 200.0
    assert reports[0].slowest_job == "d"
    assert reports[1].job_count == 2


def test_render_markdown_carries_its_own_caveat() -> None:
    """The table says how it was sampled, since the numbers drift."""
    table = render_markdown(summarize([JobTiming("x", "img", 42.0)]), "tests.yaml", 5)
    assert "| `img` | 1 | 00m42s | x | 00m42s |" in table
    assert "5 most recent successful" in table
    assert table.endswith("\n")
