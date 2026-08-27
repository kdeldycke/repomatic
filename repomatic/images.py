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
invocations of lightweight CLI tools, removing the Docker dependency.

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
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .humanize import format_file_size
from .tabular import render_markdown_table
from .tool_registry import TOOL_REGISTRY
from .tool_runner import ensure_binary

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


@cache
def _check_tool(name: str) -> bool:
    """Whether *name* can be run, from the registry or from `$PATH`.

    Memoized per process, following {func}`~repomatic.tool_runner.ensure_binary`
    and for the same reason: the optimizer loop asks once per image, and a
    `$PATH` walk per JPEG answers the same question every time.

    A tool the registry ships as a binary is always obtainable, so it never
    depends on what the machine happens to have installed:
    {func}`~repomatic.tool_runner.ensure_binary` downloads and verifies it on
    first use. A download that fails then raises rather than silently skipping
    the file, which is the intent: a pinned tool that cannot be fetched is a
    problem to surface, not to work around.

    Everything else still has to be on `$PATH`. `jpegoptim` is the only such
    tool today, because upstream publishes source tarballs rather than
    prebuilt binaries, leaving the distro package as the sole way to get it.
    """
    spec = TOOL_REGISTRY.get(name)
    if spec is not None and spec.binary is not None:
        return True
    return shutil.which(name) is not None


def _optimize_png(path: Path) -> None:
    """Optimize a PNG file in-place with `oxipng`.

    The binary comes from the `repomatic run` registry rather than `$PATH`, so
    it is the pinned version with its archive checksum verified, identical on
    every runner and every developer machine. `jpegoptim` below cannot follow:
    upstream publishes source tarballs only, leaving the distro package as the
    single way to get it.
    """
    subprocess.run(
        [
            str(ensure_binary("oxipng")),
            "--opt",
            OXIPNG_OPT_LEVEL,
            "--strip",
            "safe",
            str(path),
        ],
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

    # The backup lives outside the repository: a run killed mid-optimization
    # would otherwise strand a `.bak` beside the image, where the autofix job
    # that called this would stage it as a new file.
    with tempfile.TemporaryDirectory(prefix="repomatic-images-") as tmpdir:
        backup = Path(tmpdir) / path.name
        try:
            shutil.copy2(str(path), str(backup))
            optimizer_fn(path)
            after_bytes = path.stat().st_size

            result = OptimizationResult(
                path=path,
                before_bytes=before_bytes,
                after_bytes=after_bytes,
            )

            if (
                result.saved_pct < min_savings_pct
                or result.saved_bytes < min_savings_bytes
            ):
                # Savings too small: restore the original.
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
            # Restore the original on failure.
            if backup.exists():
                shutil.copy2(str(backup), str(path))
            logging.warning(f"Failed to optimize {path}: {exc.stderr or exc}")
            return None
        else:
            return result


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

    rows = [
        [
            f"`{r.path}`",
            format_file_size(r.before_bytes),
            format_file_size(r.after_bytes),
            f"{r.saved_pct:.1f}%",
        ]
        for r in results
    ]
    table = render_markdown_table(
        ["Filename", "Before", "After", "Improvement"],
        rows,
        align=("left", "right", "right", "right"),
    )
    headline = (
        f"Compression reduced images by **{total_pct:.1f}%**, "
        f"saving **{format_file_size(total_saved)}**."
    )
    return f"{headline}\n\n{table}"
