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
"""Tests for the git-to-release dependency source swaps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from repomatic import dep_sources
from repomatic.config import Config
from repomatic.dep_sources import (
    ReleaseSwap,
    apply_release_swaps,
    dev_floor,
    find_ready_swaps,
    floors_inside_cooldown,
    format_swap_section,
    strip_dev_bounds,
    tracked_git_overrides,
)
from repomatic.pypi import PyPIRelease

PYPROJECT = """\
[project]
name = "basket"
version = "1.0.0"
dependencies = [
  "cherry>=1.2",
  "mango[fresh]>=2.1.0.dev0; python_version >= '3.10'",
]

[project.optional-dependencies]
juice = [
  "mango[pressed]",
]

[dependency-groups]
docs = [
  "papaya>=3.0",
]
test = [
  "mango>=2.1.0.dev0",
  { include-group = "docs" },
]

[tool.uv]
exclude-newer = "1 week"
exclude-newer-package = { mango = "2026-01-05T00:00:00Z" }

[tool.uv.sources]
mango = { git = "https://github.com/acme/mango", branch = "main" }
papaya = { git = "https://github.com/acme/papaya", rev = "1234abcd" }
"""
"""A project tracking `mango`'s main branch while awaiting its `2.1.0` release.

`papaya` is pinned to an exact commit and `cherry` is a plain registry
dependency: both sit outside the managed idiom.
"""


def _write_pyproject(tmp_path: Path, content: str = PYPROJECT) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="UTF-8")
    return path


def _mock_releases(monkeypatch, releases: dict[str, dict[str, PyPIRelease]]) -> None:
    """Stub the PyPI lookup with canned release maps."""
    monkeypatch.setattr(
        dep_sources, "get_release_dates", lambda name: releases.get(name, {})
    )


def test_tracked_git_overrides_selects_branch_tracks(tmp_path):
    """Only single-source git entries with a branch are considered tracked."""
    pyproject = _write_pyproject(tmp_path)
    assert tracked_git_overrides(pyproject) == {"mango": "main"}
    # A missing file, or a project without overrides, yields nothing.
    assert tracked_git_overrides(tmp_path / "absent.toml") == {}
    bare = _write_pyproject(tmp_path, '[project]\nname = "basket"\n')
    assert tracked_git_overrides(bare) == {}


def test_dev_floor_takes_highest_dev_bound(tmp_path):
    """The floor is the highest `.dev` lower bound across every table."""
    pyproject = _write_pyproject(tmp_path)
    assert dev_floor(pyproject, "mango") == "2.1.0.dev0"
    # Any capitalization or separator style resolves to the same package.
    assert dev_floor(pyproject, "Mango") == "2.1.0.dev0"
    # Packages without a dev bound have no floor.
    assert dev_floor(pyproject, "cherry") is None
    assert dev_floor(pyproject, "papaya") is None


@pytest.mark.parametrize(
    ("requirement", "release", "expected"),
    (
        ("mango>=2.1.0.dev0", "2.1.0", "mango>=2.1.0"),
        (
            "mango[fresh]>=2.1.0.dev0; python_version >= '3.10'",
            "2.1.0",
            "mango[fresh]>=2.1.0; python_version >= '3.10'",
        ),
        ("mango>=2.1.0.dev0,<3", "2.1.0", "mango>=2.1.0,<3"),
        # A skipped version number still tightens the older dev floor.
        ("mango>=2.1.0.dev0", "2.1.1", "mango>=2.1.0"),
        # Non-dev bounds and floors awaiting a later release stay untouched.
        ("mango>=1.2", "2.1.0", "mango>=1.2"),
        ("mango>=2.2.0.dev0", "2.1.0", "mango>=2.2.0.dev0"),
        ("mango[pressed]", "2.1.0", "mango[pressed]"),
    ),
)
def test_strip_dev_bounds(requirement: str, release: str, expected: str):
    """Only matching `.dev` lower bounds are tightened, byte-for-byte."""
    assert strip_dev_bounds(requirement, release) == expected


def test_find_ready_swaps_adopts_newest_stable(tmp_path, monkeypatch):
    """A stable release satisfying the floor makes the swap ready."""
    pyproject = _write_pyproject(tmp_path)
    _mock_releases(
        monkeypatch,
        {
            "mango": {
                "2.0.0": PyPIRelease("2026-01-01", False, "mango"),
                "2.1.0": PyPIRelease("2026-07-10", False, "mango"),
                "2.1.1": PyPIRelease("2026-07-12", False, "mango"),
                "2.2.0rc1": PyPIRelease("2026-07-14", False, "mango"),
            }
        },
    )
    swaps = find_ready_swaps(pyproject)
    assert swaps == [
        ReleaseSwap(
            name="mango",
            source_key="mango",
            branch="main",
            floor="2.1.0.dev0",
            release="2.1.1",
            released="2026-07-12",
        )
    ]
    # The freeze cutoff carries a one-day margin past the release date, the
    # same convention as the automatic `_freeze_cutoff` freezes.
    assert swaps[0].freeze_cutoff == "2026-07-14T00:00:00Z"


@pytest.mark.parametrize(
    "releases",
    (
        # No release at all (unpublished package or index failure).
        {},
        # Only releases below the floor.
        {"2.0.0": PyPIRelease("2026-01-01", False, "mango")},
        # Only prereleases of the awaited version.
        {"2.1.0rc1": PyPIRelease("2026-07-10", False, "mango")},
        # The awaited release shipped but was yanked.
        {"2.1.0": PyPIRelease("2026-07-10", True, "mango")},
    ),
    ids=("no-release", "below-floor", "prerelease-only", "yanked"),
)
def test_find_ready_swaps_needs_positive_confirmation(
    tmp_path, monkeypatch, releases: dict[str, PyPIRelease]
):
    """Anything short of a live stable release keeps the git track in place."""
    pyproject = _write_pyproject(tmp_path)
    _mock_releases(monkeypatch, {"mango": releases})
    assert find_ready_swaps(pyproject) == []


def test_find_ready_swaps_skips_floorless_overrides(tmp_path, monkeypatch):
    """A branch track without a dev floor is outside the managed idiom."""
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "basket"\nversion = "1.0.0"\n'
        'dependencies = [ "mango" ]\n\n'
        "[tool.uv.sources]\n"
        'mango = { git = "https://github.com/acme/mango", branch = "main" }\n',
    )
    _mock_releases(
        monkeypatch, {"mango": {"9.9.9": PyPIRelease("2026-07-01", False, "mango")}}
    )
    assert find_ready_swaps(pyproject) == []


def test_apply_release_swaps_rewrites_pyproject(tmp_path):
    """The override is dropped and every dev floor tightened, nothing else."""
    pyproject = _write_pyproject(tmp_path)
    swap = ReleaseSwap(
        name="mango",
        source_key="mango",
        branch="main",
        floor="2.1.0.dev0",
        release="2.1.0",
        released="2026-07-10",
    )
    apply_release_swaps(pyproject, [swap])
    content = pyproject.read_text(encoding="UTF-8")
    # The override is gone; the rev-pinned sibling survives, so does the table.
    assert "mango = { git" not in content
    assert 'papaya = { git = "https://github.com/acme/papaya", rev' in content
    assert "[tool.uv.sources]" in content
    # Floors tightened in both tables, extras and markers preserved.
    assert "\"mango[fresh]>=2.1.0; python_version >= '3.10'\"" in content
    assert '"mango>=2.1.0"' in content
    assert ".dev0" not in content
    # Untouched bystanders.
    assert '"cherry>=1.2"' in content
    assert '"mango[pressed]"' in content
    assert '"papaya>=3.0"' in content


def test_apply_release_swaps_drops_emptied_sources_table(tmp_path):
    """Removing the last override removes the `[tool.uv.sources]` table too."""
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "basket"\nversion = "1.0.0"\n'
        'dependencies = [ "mango>=2.1.0.dev0" ]\n\n'
        "[tool.uv]\n"
        'exclude-newer = "1 week"\n\n'
        "[tool.uv.sources]\n"
        'mango = { git = "https://github.com/acme/mango", branch = "main" }\n',
    )
    swap = ReleaseSwap(
        name="mango",
        source_key="mango",
        branch="main",
        floor="2.1.0.dev0",
        release="2.1.0",
        released="2026-07-10",
    )
    apply_release_swaps(pyproject, [swap])
    content = pyproject.read_text(encoding="UTF-8")
    assert "sources" not in content
    assert '"mango>=2.1.0"' in content


def test_format_swap_section():
    """One row per swap, with branch, adopted release, and ship date."""
    section = format_swap_section(
        [
            ReleaseSwap(
                name="mango",
                source_key="mango",
                branch="main",
                floor="2.1.0.dev0",
                release="2.1.0",
                released="2026-07-10",
            )
        ],
        name_urls={"mango": "https://pypi.org/project/mango/"},
    )
    assert "## 🔀 Source swaps" in section
    assert "| Package | Tracked branch | Adopted | Released |" in section
    assert (
        "| [mango](https://pypi.org/project/mango/) | `main` | `2.1.0` | 2026-07-10 |"
    ) in section
    # Empty input yields nothing; an unmapped name renders plain.
    assert format_swap_section([]) == ""
    section = format_swap_section([
        ReleaseSwap("mango", "mango", "main", "2.1.0.dev0", "2.1.0", "2026-07-10")
    ])
    assert "| mango |" in section


# ---------------------------------------------------------------------------
# Dependency floors versus the install cooldown
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent

COOLDOWN_PYPROJECT = """\
[project]
name = "basket"
version = "1.0.0"
dependencies = [
  "cherry>={floor}",
]
"""

COOLDOWN_LOCK = """\
version = 1

[[package]]
name = "cherry"
version = "{locked}"

[package.sdist]
upload-time = "{upload}"
"""


@pytest.mark.parametrize(
    ("floor", "locked", "age_days", "flagged"),
    (
        # Floor demands the locked release, which is still inside the window.
        ("1.2", "1.2.0", 1, True),
        # Same floor, but the release has aged out: uvx resolves it unaided.
        ("1.2", "1.2.0", 30, False),
        # Floor predates the locked release, so an older version satisfies it.
        ("1.0", "1.2.0", 1, False),
    ),
)
def test_floors_inside_cooldown(
    tmp_path: Path,
    floor: str,
    locked: str,
    age_days: int,
    flagged: bool,
) -> None:
    """Only a floor demanding a release inside the window is reported."""
    upload = datetime.now(timezone.utc) - timedelta(days=age_days)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(COOLDOWN_PYPROJECT.format(floor=floor), encoding="UTF-8")
    lock = tmp_path / "uv.lock"
    lock.write_text(
        COOLDOWN_LOCK.format(
            locked=locked, upload=upload.isoformat().replace("+00:00", "Z")
        ),
        encoding="UTF-8",
    )

    offenders = floors_inside_cooldown(pyproject, lock, "8 days")
    assert (offenders == {"cherry": floor}) is flagged


def test_dependency_floors_clear_the_cooldown() -> None:
    """No dependency floor requires a release still inside the cooldown.

    Such a floor makes the published package uninstallable for anyone resolving
    it from an index: a downstream repo running a frozen workflow's
    `uvx 'repomatic==X.Y.Z'`, or an end user running `uvx repomatic`. Neither
    can see `uv.lock` or `[tool.uv] exclude-newer-package`, and uv has no
    environment variable for a per-package exemption, so they have nowhere to
    record a bypass.

    This repository's own CI cannot catch it, because it installs from
    `uv.lock` and resolves straight through the local exemption. That is what
    makes this a release gate rather than a CI symptom. Wait for a release to
    clear the window before raising a floor onto it.
    """
    offenders = floors_inside_cooldown(
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "uv.lock",
        Config.minimum_release_age,
    )
    listed = ", ".join(f"{name}>={floor}" for name, floor in sorted(offenders.items()))
    assert not offenders, (
        f"Dependency floor(s) inside the {Config.minimum_release_age} cooldown: "
        f"{listed}. Releasing now ships a package that downstream repos and "
        "`uvx` users cannot resolve: they see neither uv.lock nor [tool.uv] "
        "exclude-newer-package, and uv has no env var for a per-package "
        "exemption. Wait for the release to age out of the window."
    )
