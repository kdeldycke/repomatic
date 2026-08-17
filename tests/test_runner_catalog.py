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

"""The available-images table parses, and fails closed when it does not.

The fixture below is trimmed from the real `actions/runner-images` readme and
keeps every shape that has bitten: a `-latest` alias sitting *inside* a label
rather than at its end (`macos-latest-large`), a row whose plain label is the
x64 `-intel` variant rather than the bare name, badges with and without a link,
and the `<br>` endpoint markup that follows every display name.
"""

from __future__ import annotations

import pytest

from repomatic.runner_catalog import (
    RunnerImage,
    newer_version_than,
    parse_catalog,
    successor_for,
)

README = """\
# Runner Images

## Available Images

| Image | Architecture | YAML Label | Included Software |
| ------|--------------|------------|-------------------|
| Ubuntu 26.04 ![preview](https://img.shields.io/badge/preview)<br>![Endpoint Badge](https://x) | x64 | `ubuntu-26.04` | [ubuntu-26.04] |
| Ubuntu 24.04<br>![Endpoint Badge](https://x) | x64 | `ubuntu-latest` or `ubuntu-24.04` | [ubuntu-24.04] |
| Ubuntu 22.04<br>![Endpoint Badge](https://x) | x64 | `ubuntu-22.04` | [ubuntu-22.04] |
| macOS 26<br>![Endpoint Badge](https://x) | x64 | `macos-latest-large`, `macos-26-intel`, `macos-26-large` | [macOS-26] |
| macOS 26 Arm64<br>![Endpoint Badge](https://x) | arm64 | `macos-latest`, `macos-26` or `macos-26-xlarge` | [macOS-26-arm64] |
| macOS 14 Arm64 [![deprecated](https://img.shields.io/badge/deprecated)](https://github.com/actions/runner-images/issues/13518)<br>![Endpoint Badge](https://x) | arm64 | `macos-14` or `macos-14-xlarge` | [macOS-14-arm64] |

### Label scheme

- Not a table row.
"""


@pytest.fixture
def catalog() -> list[RunnerImage]:
    return parse_catalog(README)


@pytest.fixture
def by_name(catalog) -> dict[str, RunnerImage]:
    """The catalog indexed by display name, for assertion lookups."""
    return {image.display_name: image for image in catalog}


def test_every_row_is_parsed(catalog) -> None:
    """The separator row and the trailing prose are not mistaken for images."""
    assert [image.display_name for image in catalog] == [
        "Ubuntu 26.04",
        "Ubuntu 24.04",
        "Ubuntu 22.04",
        "macOS 26",
        "macOS 26 Arm64",
        "macOS 14 Arm64",
    ]


def test_no_floating_alias_survives(catalog) -> None:
    """No `-latest` label reaches a caller, wherever it sat in the cell.

    `macos-latest-large` is the one that matters: a suffix test keeps it, and a
    caller writing it into a `runs-on:` would introduce the exact floating
    alias `lint-repo` rejects.
    """
    leaked = [
        label
        for image in catalog
        for label in image.labels
        if "latest" in label.split("-")
    ]
    assert not leaked, f"floating aliases survived parsing: {leaked}"


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        # The plain hosted label wins over the paid larger runner...
        ("macOS 26 Arm64", "macos-26"),
        # ...but `-intel` is a generation's x64 half, not a size variant, so it
        # is the right answer where no barer label exists.
        ("macOS 26", "macos-26-intel"),
        ("Ubuntu 24.04", "ubuntu-24.04"),
    ],
)
def test_preferred_label(by_name, display_name: str, expected: str) -> None:
    """One label per row, picked without reaching for a larger runner."""
    assert by_name[display_name].preferred_label == expected


def test_badges_are_read_with_and_without_a_link(by_name) -> None:
    """A badge marks the state whether or not it is wrapped in a link."""
    assert by_name["Ubuntu 26.04"].preview
    assert by_name["macOS 14 Arm64"].deprecated


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # Released 24.04 wins over the newer-but-preview 26.04.
        ("ubuntu-22.04", "ubuntu-24.04"),
        # Stays inside the architecture: the Arm64 macOS row, not the x64 one.
        ("macos-14", "macos-26"),
        # A label the table does not carry resolves to nothing.
        ("ubuntu-18.04", None),
    ],
)
def test_successor_prefers_released_over_preview(
    catalog, label: str, expected: str | None
) -> None:
    """A retirement lands on released ground whenever released ground exists.

    A forced move should not trade a known deadline for an unknown one, so a
    released successor wins however old it is. `newer_preview_than` is what
    surfaces the fresher option without taking it.
    """
    successor = successor_for(label, catalog)
    assert (successor.preferred_label if successor else None) == expected


def test_successor_accepts_a_same_version_sibling(catalog) -> None:
    """A dying image with only a same-version sibling still has somewhere to go.

    Version is not filtered here, unlike {func}`newer_version_than`: staying on
    an image with an end date is worse than moving sideways to one without.
    """
    deprecated_arm = RunnerImage(
        "Windows 11 Arm64", "arm64", ("windows-11-arm",), False, True
    )
    sibling = RunnerImage(
        "Windows 11 Arm64 with Visual Studio 2026",
        "arm64",
        ("windows-11-vs2026-arm",),
        True,
        False,
    )
    successor = successor_for("windows-11-arm", [deprecated_arm, sibling])
    assert successor is not None
    assert successor.preferred_label == "windows-11-vs2026-arm"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # A genuine version bump, preview or not.
        ("ubuntu-24.04", "ubuntu-26.04"),
        # Already on the newest.
        ("ubuntu-26.04", None),
    ],
)
def test_newer_version_is_strict(catalog, label: str, expected: str | None) -> None:
    """Only a strictly higher version counts as an upgrade.

    This is what keeps a same-version flavour, like a Visual Studio variant,
    from being proposed as a newer image when it is merely a different one.
    """
    newer = newer_version_than(label, catalog)
    assert (newer.preferred_label if newer else None) == expected


@pytest.mark.parametrize(
    "readme",
    [
        pytest.param("# Runner Images\n\nNo table here.\n", id="no-table"),
        pytest.param(
            "## Available Images\n\n| Name | Arch | Label |\n| - | - | - |\n",
            id="renamed-columns",
        ),
        pytest.param(
            "| Image | Architecture | YAML Label |\n| - | - | - |\n", id="no-rows"
        ),
    ],
)
def test_parse_fails_closed(readme: str) -> None:
    """A restyled table yields nothing rather than something wrong.

    Every caller reads an empty catalog as "propose nothing". A wrong label
    would rewrite a `runs-on:` to an image GitHub does not host, failing every
    job in the repository; a missing one costs a cycle of not noticing.
    """
    assert parse_catalog(readme) == []
