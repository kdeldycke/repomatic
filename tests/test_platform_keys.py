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

"""Integrity tests for the binary release-asset naming convention.

`binary_name`, `versionless_alias` and `binary_filename_re` are the one
definition of the `{package}-{version}-{target}.{ext}` convention that the
release engine, the install-guide freeze, and the binaries catalog all build
on. These tests pin the convention against every `NUITKA_BUILD_TARGETS`
entry.
"""

from __future__ import annotations

import pytest

from repomatic.binary import (
    NUITKA_BUILD_TARGETS,
    binary_filename_re,
    binary_name,
    versionless_alias,
)

VALID_BUILD_KEYS = frozenset(NUITKA_BUILD_TARGETS)
"""Canonical set of repomatic's own binary build targets."""


@pytest.mark.parametrize("target", sorted(VALID_BUILD_KEYS))
def test_naming_round_trip_per_target(target):
    """Composed names match the pattern and alias back losslessly."""
    extension = NUITKA_BUILD_TARGETS[target].extension
    versioned = binary_name("repomatic", target, "1.2.3")
    assert versioned == f"repomatic-1.2.3-{target}.{extension}"

    match = binary_filename_re("repomatic").fullmatch(versioned)
    assert match, f"pattern did not match composed filename: {versioned}"
    assert match.group("target") == target
    assert match.group("ext") == extension

    alias = versionless_alias(versioned, "1.2.3")
    assert alias == binary_name("repomatic", target)
    # The versionless spelling matches the pattern too: the freeze rewrites
    # it back onto the versioned form.
    assert binary_filename_re("repomatic").fullmatch(alias)


def test_pattern_rejects_unknown_target():
    """The pattern must not accept platform keys outside the known set."""
    assert (
        binary_filename_re("repomatic").match("repomatic-1.0.0-freebsd-riscv128.bin")
        is None
    )


@pytest.mark.parametrize(
    ("filename", "expected"),
    (
        # A filename without the exact version segment has no alias.
        ("repomatic-linux-arm64.bin", None),
        ("repomatic-9.9.9-linux-arm64.bin", None),
        # Non-binary assets never earn aliases, whatever their name.
        ("repomatic-1.2.3-manpages.tar.gz", None),
        ("repomatic-1.2.3-linux-arm64.bin.attestation.json", None),
        ("repomatic-1.2.3-windows-x64.exe", "repomatic-windows-x64.exe"),
    ),
)
def test_versionless_alias_filters(filename, expected):
    """Only versioned compiled binaries map onto an alias."""
    assert versionless_alias(filename, "1.2.3") == expected
