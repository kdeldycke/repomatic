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

"""Which runner images exist, what they are called, and which are on the way out.

`actions/runner-images` publishes an *Available Images* table in its readme with
one row per image, carrying the display name, the architecture, the `runs-on:`
labels that reach it, and inline `preview` / `deprecated` badges. That table is
the canonical dictionary for two questions nothing else answers cleanly:

- **Which generation a label belongs to.** Deriving one from the other by
  pattern fails on macOS, where the generations disagree: `macos-14` is the
  *Arm64* image while its x64 twin is `macos-14-large`, and `macos-26-intel`
  breaks the pattern again. The display name states the family and the version
  that the labels only imply, so a successor search stays inside one operating
  system and orders its generations correctly.
- **Which images are current.** The badges mark preview and deprecated images,
  which is what makes a retirement visible before a build starts failing.

This table is the only source read. {mod}`repomatic.runner_images` carries why
it replaced the announcement feed, and what that trade costs.

```{caution}
Every parse here fails **closed**. A restyled table yields no rows, which makes
the catalog unavailable rather than wrong, and every caller treats an
unavailable catalog as "propose nothing". A wrong label would rewrite a
`runs-on:` to something GitHub does not host, which fails every job in the
repository; a missing one costs a cycle of not noticing.
```
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

from extra_platforms import AARCH64, X86_64

from .github.gh import gh_api_json

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Final

    from extra_platforms import Architecture

CATALOG_REPO = "actions/runner-images"
"""Repository whose readme carries the *Available Images* table."""

TABLE_HEADER_RE = re.compile(
    r"^\|\s*Image\s*\|\s*Architecture\s*\|\s*YAML Label\s*\|", re.MULTILINE
)
"""The table's header row, matched by column name rather than by position.

Anchoring on the names is what survives a column being added or reordered: the
row is located by what it says, and the cells below it are read by the index
this match establishes rather than by a hard-coded one.
"""

BADGE_RE = re.compile(r"!\[(?P<state>preview|deprecated)\]")
"""A status badge, identified by its alt text rather than its image URL.

The URL carries a colour and a style that GitHub restyles freely; the alt text
is the word a reader sees and has stayed put across restyles.
"""

LABEL_RE = re.compile(r"`([a-z][a-z0-9.\-]*)`")
"""A `runs-on:` label, backticked inside the *YAML Label* cell.

The cell separates alternatives in prose ("`macos-latest`, `macos-26` or
`macos-26-xlarge`"), so the backticks are what delimit a label rather than the
punctuation around them.
"""

LATEST_TOKEN = "latest"
"""Hyphen-separated part marking a floating alias, dropped on sight.

Tested per part rather than as a suffix, because the alias is not always
trailing: the x64 macOS row offers `macos-latest-large` beside `macos-26-intel`,
and a `-latest$` test keeps the very label `lint-repo` rejects. GitHub repoints
these with no commit to review, so filtering here means no caller can propose
one by accident.
"""

SIZED_SUFFIXES = ("-large", "-xlarge")
"""macOS size variants, deprioritized when picking one label from a row.

A row often lists an ordinary hosted label beside sized ones (`macos-26`
against `macos-26-xlarge`, `macos-26-intel` against `macos-26-large`). The
sized ones are the paid larger runners, so they are never the default. Note
that `-intel` is *not* a size variant: it is the x64 half of a macOS
generation, and the label this project runs.
"""

CATALOG_ARCHITECTURES: Final[dict[str, Architecture]] = {
    "arm64": AARCH64,
    "x64": X86_64,
}
"""The *Architecture* column's own spelling, mapped to extra-platforms.

{attr}`RunnerImage.architecture` keeps the table's wording, so a comparison
against anything else in this project has to come through here.
"""

ARCH_SUFFIXES: Final[tuple[tuple[str, Architecture], ...]] = (
    ("-arm", AARCH64),
    ("-xlarge", AARCH64),
    ("-large", X86_64),
    ("-intel", X86_64),
)
"""Label suffixes that settle an architecture on their own.

The macOS size suffixes are why this cannot be one rule: they invert what the
names suggest, since `macos-15-large` is x64 while `macos-15-xlarge` is Arm64.
`-intel` names the x64 half of a generation ({data}`SIZED_SUFFIXES` says why it
is not a size), and `-arm` covers the Linux (`ubuntu-26.04-arm`) and Windows
(`windows-11-arm`) Arm images alike.

Order is for reading only: `-large` cannot match a label ending in `-xlarge`,
because the two differ in the character before `large`.
"""

APPLE_SILICON_MACOS_FLOOR: Final[int] = 14
"""First macOS generation whose bare label is Apple silicon.

GitHub moved the default macOS image to Apple silicon at `macos-14`, so a bare
label from this generation on is Arm64 and an older one is x64. A suffixed
label never reaches this comparison: {data}`ARCH_SUFFIXES` settles it first.
"""


@dataclass(frozen=True)
class RunnerImage:
    """One row of the *Available Images* table."""

    display_name: str
    """Name as the table writes it, badges and endpoint markup stripped.

    The only place the operating system and the generation are spelled out, so
    {attr}`family` and {attr}`version` both read it rather than the labels.
    """

    architecture: str
    """`x64` or `arm64`, as the table's own column spells it."""

    labels: tuple[str, ...]
    """Every `runs-on:` label reaching this image, `-latest` aliases removed."""

    preview: bool
    """Whether the row is badged as a public preview."""

    deprecated: bool
    """Whether the row is badged as deprecated."""

    @property
    def family(self) -> str:
        """Leading word of the display name: `Ubuntu`, `macOS` or `Windows`.

        Used to keep a successor search inside one operating system, which the
        labels alone cannot express (`macos-26-intel` and `macos-26` share a
        family that no common label prefix captures).
        """
        return self.display_name.split()[0] if self.display_name else ""

    @property
    def version(self) -> tuple[int, ...]:
        """Numeric version read out of the display name, for ordering.

        `Ubuntu 26.04 Arm64` sorts above `Ubuntu 24.04`, and a name carrying no
        number at all (`Ubuntu Slim`) sorts below every numbered sibling rather
        than raising.
        """
        match = re.search(r"(\d+(?:\.\d+)*)", self.display_name)
        if not match:
            return ()
        return tuple(int(part) for part in match.group(1).split("."))

    @property
    def preferred_label(self) -> str:
        """The one label to write into a `runs-on:` for this image.

        Prefers a plain label over a sized variant, so a row offering
        `macos-26` beside `macos-26-xlarge` yields the ordinary hosted runner.
        """
        plain = [
            label for label in self.labels if not label.endswith(SIZED_SUFFIXES)
        ] or list(self.labels)
        # Shortest wins among the survivors: a generation's plain label is
        # always a prefix-length subset of its decorated siblings
        # (`windows-2025` against `windows-2025-vs2026`).
        return min(plain, key=len, default="")


def _split_row(line: str) -> list[str]:
    """Split a Markdown table row into its cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_catalog(readme: str) -> list[RunnerImage]:
    """Read the *Available Images* table out of a readme.

    :param readme: Full Markdown source of the `actions/runner-images` readme.
    :return: One {class}`RunnerImage` per table row, empty when the table
        cannot be located or yields no usable row.
    """
    header = TABLE_HEADER_RE.search(readme)
    if not header:
        logging.warning("Available Images table not found: catalog unavailable.")
        return []

    columns = _split_row(readme[header.start() : header.end()].rstrip("|"))
    try:
        name_at = columns.index("Image")
        arch_at = columns.index("Architecture")
        label_at = columns.index("YAML Label")
    except ValueError:
        logging.warning("Available Images columns moved: catalog unavailable.")
        return []

    images: list[RunnerImage] = []
    # The header match ends at its third pipe, mid-line, so rows start after
    # that line's own newline rather than at the match's end.
    row_start = readme.find("\n", header.end())
    if row_start < 0:
        return []
    for line in readme[row_start + 1 :].splitlines():
        if not line.startswith("|"):
            # The table ends at the first non-row line, so a later pipe
            # character elsewhere in the readme is never read as a row.
            break
        cells = _split_row(line)
        if len(cells) <= max(name_at, arch_at, label_at):
            continue
        raw_name = cells[name_at]
        # The separator row under the header is all dashes and yields no name.
        if set(raw_name) <= {"-", " ", ":"}:
            continue
        labels = tuple(
            label
            for label in LABEL_RE.findall(cells[label_at])
            if LATEST_TOKEN not in label.split("-")
        )
        if not labels:
            continue
        states = set(BADGE_RE.findall(raw_name))
        # Everything before the first badge or line break is the bare name; the
        # rest is badge and endpoint markup.
        display_name = re.split(r"\s*(?:\[?!\[|<br>)", raw_name)[0].strip()
        images.append(
            RunnerImage(
                display_name=display_name,
                architecture=cells[arch_at],
                labels=labels,
                preview="preview" in states,
                deprecated="deprecated" in states,
            )
        )

    if not images:
        logging.warning(
            "Available Images table parsed to nothing: catalog unavailable."
        )
    return images


def fetch_catalog(repo: str = CATALOG_REPO) -> list[RunnerImage]:
    """Download and parse the *Available Images* table.

    Read through `gh` rather than a bare HTTP GET: the authenticated path
    carries a rate limit a CI job will not exhaust, where the anonymous one
    shares 60 requests an hour with every other job on the runner.

    :param repo: Repository whose readme to read.
    :return: The catalog, empty when the readme could not be read or parsed.
        Callers treat an empty catalog as "propose nothing" rather than as
        "nothing exists".
    """
    payload = gh_api_json(["api", f"repos/{repo}/readme"])
    if not isinstance(payload, dict):
        logging.warning(f"Could not read the {repo} readme.")
        return []
    try:
        readme = base64.b64decode(payload.get("content", "")).decode("UTF-8")
    except (ValueError, UnicodeDecodeError):
        logging.warning(f"The {repo} readme did not decode.")
        return []
    return parse_catalog(readme)


def by_label(catalog: Sequence[RunnerImage]) -> dict[str, RunnerImage]:
    """Index a catalog by every label reaching each image."""
    return {label: image for image in catalog for label in image.labels}


def live_siblings(
    current: RunnerImage, catalog: Sequence[RunnerImage]
) -> list[RunnerImage]:
    """Every image that could host a job currently on *current*.

    Same operating system and architecture, not itself, and not on its way out.
    Version is deliberately *not* filtered: a dying image whose family offers
    only a same-version sibling still has somewhere to go, and going there beats
    staying on a deadline.

    :param current: The image being moved off.
    :param catalog: Parsed catalog.
    :return: Candidates, unordered.
    """
    return [
        image
        for image in catalog
        if image.family == current.family
        and image.architecture == current.architecture
        and image.display_name != current.display_name
        and not image.deprecated
    ]


def successor_for(label: str, catalog: Sequence[RunnerImage]) -> RunnerImage | None:
    """The image a workflow on *label* should move to when its own is retiring.

    Prefers a released image over a preview, then the highest version. The
    ordering matters more than it looks: a retirement is a forced move, and
    landing it on something GitHub is still rolling out trades a known deadline
    for an unknown one. So a released successor always wins, however old.

    A preview is still returned when the family offers nothing else, because the
    alternative is proposing nothing and leaving the job on an image with an
    end date. {func}`newer_preview_than` surfaces the preview separately when a
    released successor was chosen, so a reviewer sees the fresher option
    without it being taken on their behalf.

    :param label: Label whose image is retiring, or has vanished.
    :param catalog: Parsed catalog.
    :return: The best replacement, or `None` when the family offers none.
    """
    current = by_label(catalog).get(label)
    if not current:
        return None
    return max(
        live_siblings(current, catalog),
        key=lambda image: (not image.preview, image.version),
        default=None,
    )


def newer_preview_than(
    chosen: RunnerImage, current: RunnerImage, catalog: Sequence[RunnerImage]
) -> RunnerImage | None:
    """A preview image newer than the one {func}`successor_for` settled on.

    Reported rather than adopted. Whether a fresher preview beats a released
    image is a capacity-and-risk judgement the pull request exists to host, so
    naming the alternative in the body is the useful half; picking it is not.

    :param chosen: What `successor_for` returned.
    :param current: The image being moved off.
    :param catalog: Parsed catalog.
    :return: The newest preview above *chosen*, or `None`.
    """
    if chosen.preview:
        return None
    previews = [
        image
        for image in live_siblings(current, catalog)
        if image.preview and image.version > chosen.version
    ]
    return max(previews, key=lambda image: image.version, default=None)


def newer_version_than(
    label: str, catalog: Sequence[RunnerImage]
) -> RunnerImage | None:
    """A genuinely newer *version* of the image behind *label*, if one exists.

    Strictly newer by version, which is what separates an upgrade from a
    flavour. `Windows 11 Arm64 with Visual Studio 2026` sits at the same version
    as `Windows 11 Arm64` and is a different toolchain rather than a newer
    image, so it is not an upgrade and is not reported as one.

    :param label: Label currently in use.
    :param catalog: Parsed catalog.
    :return: The newest strictly-higher version available, or `None`.
    """
    current = by_label(catalog).get(label)
    if not current:
        return None
    newer = [
        image
        for image in live_siblings(current, catalog)
        if image.version > current.version
    ]
    return max(newer, key=lambda image: image.version, default=None)


def runner_architecture(label: str) -> Architecture:
    """Returns the architecture a `runs-on:` label runs on, without the catalog.

    The table above is the authority, but reading it costs a network call, so
    nothing that runs inside a job can wait on it. This derives the same answer
    from the label's own shape, which is fixed enough to state: a suffix from
    {data}`ARCH_SUFFIXES`, else Apple silicon for a macOS generation at or above
    {data}`APPLE_SILICON_MACOS_FLOOR`, else x64.

    Answering from the shape is also what lets it speak for a label this project
    has never listed, which a fixed table cannot: a downstream repository adds
    its own images through `[tool.repomatic.test-matrix] variations.os`.

    ```{caution}
    Unlike every parse in this module, this one does not fail closed: an
    unrecognized label reads as x64 rather than as "unknown", because there is
    no third answer a caller could act on. `tests/test_runner_catalog.py` pins
    the derivation against the published table, so a new label shape GitHub
    introduces fails the suite instead of being guessed at silently.
    ```

    :param label: A `runs-on:` label, like `ubuntu-26.04-arm` or `macos-15`.
    :return: The [Extra Platforms](https://kdeldycke.github.io/extra-platforms)
        architecture, matching how {mod}`repomatic.release.binary` names one.
    """
    for suffix, arch in ARCH_SUFFIXES:
        if label.endswith(suffix):
            return arch

    family, _, generation = label.partition("-")
    # Every Xcode image is Apple silicon.
    if family == "xcode":
        return AARCH64
    # A Linux or Windows image is x64 unless a suffix above said otherwise.
    if family != "macos":
        return X86_64
    major = generation.split(".")[0]
    # `macos-latest` tracks GitHub's current default, Apple silicon since
    # `macos-14`. Filtered out of the catalog by LATEST_TOKEN, but reachable
    # here because a caller may pass any label it finds in a workflow.
    if not major.isdigit():
        return AARCH64
    return AARCH64 if int(major) >= APPLE_SILICON_MACOS_FLOOR else X86_64
