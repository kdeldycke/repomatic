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

"""Tests for image optimization utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from repomatic.images import (
    OptimizationResult,
    format_file_size,
    generate_markdown_summary,
    optimize_image,
)


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1,023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (10240, "10.0 KB"),
        (1048576, "1.0 MB"),
        (1572864, "1.5 MB"),
        (1073741824, "1.0 GB"),
    ],
)
def test_format_file_size(size_bytes: int, expected: str) -> None:
    """Human-readable file size formatting."""
    assert format_file_size(size_bytes) == expected


def test_optimization_result_saved_bytes() -> None:
    """Saved bytes is the difference between before and after."""
    result = OptimizationResult(
        path=Path("test.png"), before_bytes=1000, after_bytes=800
    )
    assert result.saved_bytes == 200


def test_optimization_result_saved_pct() -> None:
    """Saved percentage is computed from before/after."""
    result = OptimizationResult(
        path=Path("test.png"), before_bytes=1000, after_bytes=800
    )
    assert result.saved_pct == pytest.approx(20.0)


def test_optimization_result_zero_before() -> None:
    """Zero-byte file produces 0% savings."""
    result = OptimizationResult(path=Path("test.png"), before_bytes=0, after_bytes=0)
    assert result.saved_pct == 0.0


def test_generate_markdown_summary_empty() -> None:
    """Empty results produce a simple message."""
    assert generate_markdown_summary([]) == "No images were optimized."


def test_generate_markdown_summary_single_result() -> None:
    """Summary table with a single result."""
    results = [
        OptimizationResult(path=Path("logo.png"), before_bytes=10240, after_bytes=8192),
    ]
    md = generate_markdown_summary(results)
    assert "**20.0%**" in md
    assert "**2.0 KB**" in md
    assert "| `logo.png`" in md
    assert "| Filename | Before | After | Improvement |" in md


def test_generate_markdown_summary_multiple_results() -> None:
    """Summary table with multiple results shows totals."""
    results = [
        OptimizationResult(path=Path("a.png"), before_bytes=10000, after_bytes=8000),
        OptimizationResult(path=Path("b.jpg"), before_bytes=20000, after_bytes=15000),
    ]
    md = generate_markdown_summary(results)
    # Total: 30000 → 23000 = 7000 saved = 23.3%.
    assert "**23.3%**" in md
    assert "| `a.png`" in md
    assert "| `b.jpg`" in md


def test_optimize_image_missing_tool(tmp_path: Path) -> None:
    """Returns None when the required tool is not on $PATH."""
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 100)
    with patch("repomatic.images._check_tool", return_value=False):
        result = optimize_image(img, min_savings_pct=5)
    assert result is None


def test_optimize_image_zero_byte_file(tmp_path: Path) -> None:
    """Returns None for empty files."""
    img = tmp_path / "empty.png"
    img.write_bytes(b"")
    result = optimize_image(img, min_savings_pct=5)
    assert result is None


def test_optimize_image_unsupported_extension(tmp_path: Path) -> None:
    """Returns None for unsupported file extensions."""
    img = tmp_path / "test.bmp"
    img.write_bytes(b"\x00" * 100)
    result = optimize_image(img, min_savings_pct=5)
    assert result is None


def _patch_optimizer(ext, mock_fn):
    """Temporarily replace the optimizer function for an extension in OPTIMIZERS."""
    from repomatic.images import OPTIMIZERS

    original = OPTIMIZERS[ext]
    return patch.dict(OPTIMIZERS, {ext: (original[0], mock_fn)})


def test_optimize_image_below_threshold(tmp_path: Path) -> None:
    """Restores original when savings are below threshold."""
    img = tmp_path / "test.png"
    original_data = b"\x89PNG" + b"\x00" * 100
    img.write_bytes(original_data)

    def mock_optimize(path: Path) -> None:
        # Only remove 1 byte — well below 5% threshold.
        path.write_bytes(original_data[:-1])

    with (
        patch("repomatic.images._check_tool", return_value=True),
        _patch_optimizer(".png", mock_optimize),
    ):
        result = optimize_image(img, min_savings_pct=5)

    assert result is None
    # Original file should be restored.
    assert img.read_bytes() == original_data


def test_optimize_image_above_threshold(tmp_path: Path) -> None:
    """Returns result when savings exceed threshold."""
    img = tmp_path / "test.png"
    original_data = b"\x89PNG" + b"\x00" * 100

    img.write_bytes(original_data)

    def mock_optimize(path: Path) -> None:
        # Remove 50% of the data.
        path.write_bytes(original_data[: len(original_data) // 2])

    with (
        patch("repomatic.images._check_tool", return_value=True),
        _patch_optimizer(".png", mock_optimize),
    ):
        result = optimize_image(img, min_savings_pct=5)

    assert result is not None
    assert result.before_bytes == len(original_data)
    assert result.after_bytes == len(original_data) // 2
    assert result.saved_pct == pytest.approx(50.0, rel=0.1)


def test_optimize_image_restores_on_failure(tmp_path: Path) -> None:
    """Original file is restored when the optimizer tool fails."""
    import subprocess

    img = tmp_path / "test.jpg"
    original_data = b"\xff\xd8\xff" + b"\x00" * 100
    img.write_bytes(original_data)

    def mock_fail(path: Path) -> None:
        raise subprocess.CalledProcessError(1, "jpegoptim", stderr="error")

    with (
        patch("repomatic.images._check_tool", return_value=True),
        _patch_optimizer(".jpg", mock_fail),
    ):
        result = optimize_image(img, min_savings_pct=5)

    assert result is None
    # Original file should be restored.
    assert img.read_bytes() == original_data
