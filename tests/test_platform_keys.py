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

"""Cross-module integrity tests for platform target keys.

``BINARY_ARCH_MAPPINGS``, ``NUITKA_BUILD_TARGETS``, and the binary filename regex
in ``prepare_release`` all encode the same set of platform targets for repomatic's own
binary builds. These tests enforce that they stay in sync.
"""

from __future__ import annotations

import re

import pytest

from repomatic.binary import BINARY_ARCH_MAPPINGS, NUITKA_BUILD_TARGETS

# The regex from ReleasePrep.freeze_install_download_urls, extracted here so the
# test breaks loudly if the pattern is changed without updating this file.
BINARY_FILENAME_RE = re.compile(
    r"repomatic(?:-[\d.]+)?-"
    r"((?:linux|macos|windows)-(?:arm64|x64))\.(bin|exe)",
)

VALID_BUILD_KEYS = frozenset(NUITKA_BUILD_TARGETS)
"""Canonical set of repomatic's own binary build targets."""


def test_all_constants_share_same_keys():
    """Build-target constants must define the exact same set of keys."""
    binary_keys = set(BINARY_ARCH_MAPPINGS)
    nuitka_keys = set(NUITKA_BUILD_TARGETS)

    assert binary_keys == nuitka_keys, (
        "BINARY_ARCH_MAPPINGS vs NUITKA_BUILD_TARGETS mismatch: "
        f"only in binary={binary_keys - nuitka_keys}, "
        f"only in nuitka={nuitka_keys - binary_keys}"
    )


@pytest.mark.parametrize("target", sorted(VALID_BUILD_KEYS))
def test_binary_filename_regex_matches_all_targets(target):
    """The prepare-release regex must match a filename for every known target."""
    extension = NUITKA_BUILD_TARGETS[target]["extension"]
    filename = f"repomatic-1.2.3-{target}.{extension}"
    match = BINARY_FILENAME_RE.match(filename)
    assert match, f"regex did not match filename: {filename}"
    assert match.group(1) == target
    assert match.group(2) == extension


def test_binary_filename_regex_rejects_unknown_target():
    """The regex must not accept platform keys outside the known set."""
    match = BINARY_FILENAME_RE.match("repomatic-1.0.0-freebsd-riscv128.bin")
    assert match is None
