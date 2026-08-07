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

"""Image optimization using external CLI tools.

Replaces the Docker-based `calibreapp/image-actions` GitHub Action with direct
invocations of lightweight CLI tools, removing the Docker dependency and enabling
`ubuntu-slim` runners.

Tools used per format:

- PNG: `oxipng` (lossless, multithreaded Rust optimizer).
- JPEG/JPG: `jpegoptim` (lossless Huffman optimization + metadata stripping).

```{note}
Both tools are strictly **lossless**: `oxipng` finds optimal PNG encoding
parameters without altering pixel data, and `jpegoptim` (without `-m`)
rewrites Huffman tables only. This means optimization is **idempotent** — a
second run produces no further changes, so the workflow never creates noisy
PRs for negligible savings.
```

```{warning}
WebP and AVIF are intentionally **not** optimized. The only available tools
(`cwebp`, `avifenc`) work by lossy re-encoding: decode → re-compress at
a target quality. This is **not idempotent** — each pass re-compresses the
previous output, producing progressively smaller (and worse) files. The
earlier `calibreapp/image-actions` suffered from this: it required multiple
workflow runs to stabilize below the savings threshold, generating repeated
PRs with diminishing returns and cumulative quality loss. Lossless WebP/AVIF
modes exist but typically *increase* file size when applied to already
lossy-encoded images, making them counterproductive. Since WebP and AVIF are
modern formats chosen specifically for their compression efficiency, files in
these formats are almost always already well-optimized at creation time.
```
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from click_extra import format_size

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Minimum percentage savings required to keep the optimized file.
DEFAULT_MIN_SAVINGS_PCT = 5

# Minimum absolute byte savings required to keep the optimized file.
DEFAULT_MIN_SAVINGS_BYTES = 1024

OXIPNG_OPT_LEVEL = "4"
JPEGOPTIM_FLAGS = ("--strip-all", "--all-progressive")


@dataclass
class OptimizationResult:
    """Result of optimizing a single image file."""

    path: Path
    before_bytes: int
    after_bytes: int

    @property
    def saved_bytes(self) -> int:
        """Bytes saved by optimization."""
        return self.before_bytes - self.after_bytes

    @property
    def saved_pct(self) -> float:
        """Percentage saved, as a float 0–100."""
        if self.before_bytes == 0:
            return 0.0
        return (self.saved_bytes / self.before_bytes) * 100


def format_file_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string.

    A thin binding of {func}`click_extra.format_size` to the JEDEC unit style
    (binary powers with the customary `KB`/`MB` symbols), matching the format
    produced by `calibreapp/image-actions`.
    """
    return format_size(size_bytes, units="jedec")


def _check_tool(name: str) -> bool:
    """Return `True` if *name* is found on `$PATH`."""
    return shutil.which(name) is not None


def _optimize_png(path: Path) -> None:
    """Optimize a PNG file in-place with `oxipng`."""
    subprocess.run(
        ["oxipng", "--opt", OXIPNG_OPT_LEVEL, "--strip", "safe", str(path)],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=True,
    )


def _optimize_jpeg(path: Path) -> None:
    """Optimize a JPEG file in-place with `jpegoptim`."""
    subprocess.run(
        ["jpegoptim", *JPEGOPTIM_FLAGS, str(path)],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=True,
    )


# Map file extensions to their optimizer function and required tool name.
OPTIMIZERS: dict[str, tuple[str, Callable[[Path], None]]] = {
    ".png": ("oxipng", _optimize_png),
    ".jpg": ("jpegoptim", _optimize_jpeg),
    ".jpeg": ("jpegoptim", _optimize_jpeg),
}


def optimize_image(
    path: Path,
    min_savings_pct: float,
    min_savings_bytes: int = DEFAULT_MIN_SAVINGS_BYTES,
) -> OptimizationResult | None:
    """Optimize a single image file in-place.

    :param path: Path to the image file.
    :param min_savings_pct: Minimum percentage savings to keep the result.
        If savings are below this threshold, the original file is restored.
    :param min_savings_bytes: Minimum absolute byte savings to keep the result.
        Prevents noisy diffs for tiny files where even a high percentage
        represents negligible absolute savings.
    :return: An {class}`OptimizationResult` if the file was optimized, or
        `None` if the format is unsupported, the required tool is missing,
        or savings were below the threshold.
    """
    ext = path.suffix.lower()
    entry = OPTIMIZERS.get(ext)
    if not entry:
        logging.warning(f"No optimizer for {ext!r}: {path}")
        return None

    tool_name, optimizer_fn = entry

    if not _check_tool(tool_name):
        logging.warning(f"{tool_name!r} not found on $PATH, skipping {path}")
        return None

    before_bytes = path.stat().st_size
    if before_bytes == 0:
        return None

    # Keep a backup so we can restore if savings are below threshold.
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(str(path), str(backup))
        optimizer_fn(path)
        after_bytes = path.stat().st_size

        result = OptimizationResult(
            path=path,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
        )

        if result.saved_pct < min_savings_pct or result.saved_bytes < min_savings_bytes:
            # Savings too small — restore original.
            shutil.copy2(str(backup), str(path))
            if result.saved_bytes < min_savings_bytes:
                logging.info(
                    f"Skipped {path}: {format_file_size(result.saved_bytes)} saved "
                    f"< {format_file_size(min_savings_bytes)} threshold."
                )
            else:
                logging.info(
                    f"Skipped {path}: {result.saved_pct:.1f}% savings "
                    f"< {min_savings_pct}% threshold."
                )
            return None

        logging.info(
            f"Optimized {path}: "
            f"{format_file_size(before_bytes)} → {format_file_size(after_bytes)} "
            f"({result.saved_pct:.1f}% savings)."
        )

    except subprocess.CalledProcessError as exc:
        # Restore original on failure.
        if backup.exists():
            shutil.copy2(str(backup), str(path))
        logging.warning(f"Failed to optimize {path}: {exc.stderr or exc}")
        return None
    else:
        return result
    finally:
        backup.unlink(missing_ok=True)


def optimize_images(
    image_files: Sequence[Path],
    min_savings_pct: float = DEFAULT_MIN_SAVINGS_PCT,
    min_savings_bytes: int = DEFAULT_MIN_SAVINGS_BYTES,
) -> list[OptimizationResult]:
    """Optimize a list of image files.

    :param image_files: Paths to image files.
    :param min_savings_pct: Minimum percentage savings to keep an optimization.
    :param min_savings_bytes: Minimum absolute byte savings to keep an optimization.
    :return: List of results for files that were successfully optimized.
    """
    results = []
    for path in image_files:
        result = optimize_image(
            path,
            min_savings_pct=min_savings_pct,
            min_savings_bytes=min_savings_bytes,
        )
        if result is not None:
            results.append(result)
    # Sort by bytes saved descending (largest savings first).
    results.sort(key=lambda r: r.saved_bytes, reverse=True)
    return results


def generate_markdown_summary(results: list[OptimizationResult]) -> str:
    """Generate a markdown summary table of optimization results.

    Produces a table similar to `calibreapp/image-actions` output, showing
    before/after sizes and percentage improvement for each optimized file.
    """
    if not results:
        return "No images were optimized."

    total_before = sum(r.before_bytes for r in results)
    total_after = sum(r.after_bytes for r in results)
    total_saved = total_before - total_after
    total_pct = (total_saved / total_before * 100) if total_before else 0

    lines = [
        (
            f"Compression reduced images by **{total_pct:.1f}%**, "
            f"saving **{format_file_size(total_saved)}**."
        ),
        "",
        "| Filename | Before | After | Improvement |",
        "| :------- | -----: | ----: | ----------: |",
    ]

    lines.extend(
        f"| `{r.path}` "
        f"| {format_file_size(r.before_bytes)} "
        f"| {format_file_size(r.after_bytes)} "
        f"| {r.saved_pct:.1f}% |"
        for r in results
    )

    return "\n".join(lines)
