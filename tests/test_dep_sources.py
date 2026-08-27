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
"""Tests for the git-to-release dependency source swaps and the release gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from repomatic.config import Config
from repomatic.deps import dep_sources
from repomatic.deps.dep_sources import (
    BLOCKER_SECTION_NOTE,
    RELEASE_READY_SENTENCE,
    UNSHIPPABLE_BANNER_LEAD,
    ReleaseSwap,
    SourceKind,
    apply_release_swaps,
    build_release_readiness,
    dev_floor,
    find_ready_swaps,
    floors_inside_cooldown,
    format_swap_section,
    scan_project,
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
  "{requirement}",
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
    ("requirement", "floor", "locked", "age_days", "flagged"),
    (
        # Floor demands the locked release, which is still inside the window.
        ("cherry>=1.2", "1.2", "1.2.0", 1, True),
        # Same floor, but the release has aged out: uvx resolves it unaided.
        ("cherry>=1.2", "1.2", "1.2.0", 30, False),
        # Floor predates the locked release, so an older version satisfies it.
        ("cherry>=1.0", "1.0", "1.2.0", 1, False),
        # An exclusive bound is a lower bound too. This exercises the stale-lock
        # corner (a floor excluding the version still locked): with a fresh lock
        # the locked version always strictly exceeds a `>` floor, so the narrower
        # fresh-lock case cannot fire, and this guard is deliberately about the
        # operator set rather than that arithmetic.
        ("cherry>1.2", "1.2", "1.2.0", 1, True),
    ),
)
def test_floors_inside_cooldown(
    tmp_path: Path,
    requirement: str,
    floor: str,
    locked: str,
    age_days: int,
    flagged: bool,
) -> None:
    """Only a floor demanding a release inside the window is reported."""
    upload = datetime.now(timezone.utc) - timedelta(days=age_days)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        COOLDOWN_PYPROJECT.format(requirement=requirement), encoding="UTF-8"
    )
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


# ---------------------------------------------------------------------------
# Shippability gate (`lint-deps`)
# ---------------------------------------------------------------------------

SHIPPABLE_PYPROJECT = """\
[build-system]
requires = [ "uv-build>=0.8" ]

[project]
name = "basket"
version = "1.0.0"
dependencies = [ "cherry>=1.2" ]

[project.optional-dependencies]
juice = [ "papaya>=3" ]

[dependency-groups]
test = [ "mango>=2" ]
"""
"""A project whose every dependency resolves from PyPI.

Each rule below appends its own offending declaration to this base, so a
finding can only come from what the test added.
"""


def _scan(tmp_path: Path, extra: str = "", allow: dict[str, str] | None = None):
    """Scan a copy of `SHIPPABLE_PYPROJECT` with *extra* appended."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(SHIPPABLE_PYPROJECT + extra, encoding="UTF-8")
    return scan_project(pyproject, tmp_path / "uv.lock", "1 week", allow=allow)


def test_clean_project_reports_nothing(tmp_path: Path) -> None:
    """A project resolving everything from PyPI has no findings."""
    assert _scan(tmp_path) == []


@pytest.mark.parametrize(
    ("extra", "package", "kind"),
    (
        pytest.param(
            '[tool.uv.sources]\ncherry = { git = "https://x/cherry", branch = "main" }\n',
            "cherry",
            SourceKind.GIT,
            id="git-branch",
        ),
        pytest.param(
            '[tool.uv.sources]\ncherry = { git = "https://x/cherry", rev = "abc123" }\n',
            "cherry",
            SourceKind.GIT,
            id="git-rev",
        ),
        pytest.param(
            '[tool.uv.sources]\ncherry = { git = "https://x/cherry", tag = "v1.2" }\n',
            "cherry",
            SourceKind.GIT,
            id="git-tag",
        ),
        pytest.param(
            '[tool.uv.sources]\ncherry = { path = "../cherry" }\n',
            "cherry",
            SourceKind.PATH,
            id="path",
        ),
        pytest.param(
            '[tool.uv.sources]\ncherry = { path = "../cherry", editable = true }\n',
            "cherry",
            SourceKind.PATH,
            id="editable-path",
        ),
        pytest.param(
            '[tool.uv.sources]\ncherry = { url = "https://x/cherry-1.2-py3-none-any.whl" }\n',
            "cherry",
            SourceKind.URL,
            id="url",
        ),
        pytest.param(
            "[tool.uv.sources]\ncherry = { workspace = true }\n",
            "cherry",
            SourceKind.WORKSPACE,
            id="workspace",
        ),
        pytest.param(
            '[[tool.uv.index]]\nname = "internal"\nurl = "https://x/simple"\n'
            '[tool.uv.sources]\ncherry = { index = "internal" }\n',
            "cherry",
            SourceKind.INDEX,
            id="private-index",
        ),
        pytest.param(
            '[[tool.uv.index]]\nname = "mirror"\nurl = "https://x/simple"\ndefault = true\n',
            "mirror",
            SourceKind.INDEX,
            id="non-pypi-default-index",
        ),
    ),
)
def test_unshippable_sources_block(
    tmp_path: Path,
    extra: str,
    package: str,
    kind: SourceKind,
) -> None:
    """Every way of resolving a dependency off-index blocks a release."""
    findings = _scan(tmp_path, extra)
    assert [(f.package, f.kind, f.blocking) for f in findings] == [
        (package, kind, True)
    ]


@pytest.mark.parametrize(
    "table",
    (
        "project.dependencies",
        "project.optional-dependencies.juice",
        "dependency-groups.test",
        "build-system.requires",
    ),
)
def test_direct_references_block_in_every_table(tmp_path: Path, table: str) -> None:
    """A PEP 508 direct reference blocks wherever it is declared.

    The `v5.0.0` release shipped one in an extra, which PyPI refused at
    upload after the tag and the GitHub release had already been created.
    """
    direct = "melon @ git+https://x/melon@fix-ripening"
    doc = SHIPPABLE_PYPROJECT.replace(
        {
            "project.dependencies": '[ "cherry>=1.2" ]',
            "project.optional-dependencies.juice": '[ "papaya>=3" ]',
            "dependency-groups.test": '[ "mango>=2" ]',
            "build-system.requires": '[ "uv-build>=0.8" ]',
        }[table],
        f'[ "{direct}" ]',
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(doc, encoding="UTF-8")
    findings = scan_project(pyproject, tmp_path / "uv.lock", "1 week")
    assert [(f.package, f.kind, f.location, f.blocking) for f in findings] == [
        ("melon", SourceKind.DIRECT_REFERENCE, table, True)
    ]


def test_per_platform_source_list_reports_once_per_kind(tmp_path: Path) -> None:
    """A marker-split override is one finding, not one per platform."""
    extra = (
        "[tool.uv.sources]\n"
        "cherry = [\n"
        '  { git = "https://x/cherry", branch = "main",'
        " marker = \"sys_platform == 'linux'\" },\n"
        '  { git = "https://x/cherry", branch = "main",'
        " marker = \"sys_platform == 'darwin'\" },\n"
        "]\n"
    )
    findings = _scan(tmp_path, extra)
    assert [(f.package, f.kind) for f in findings] == [("cherry", SourceKind.GIT)]


def test_pypi_element_does_not_mask_a_private_one(tmp_path: Path) -> None:
    """Deduplication must not let a clean list element hide a dirty one.

    Both elements classify as an index source, and the PyPI one is skipped
    without reporting. Recording it as seen anyway would swallow the private
    index that follows it.
    """
    extra = (
        '[[tool.uv.index]]\nname = "pypi"\nurl = "https://pypi.org/simple"\n'
        '[[tool.uv.index]]\nname = "internal"\nurl = "https://x/simple"\n'
        "[tool.uv.sources]\n"
        "cherry = [\n"
        '  { index = "pypi", marker = "sys_platform == \'linux\'" },\n'
        '  { index = "internal", marker = "sys_platform == \'darwin\'" },\n'
        "]\n"
    )
    findings = _scan(tmp_path, extra)
    assert [(f.package, f.kind) for f in findings] == [("cherry", SourceKind.INDEX)]
    assert "internal" in findings[0].detail


def test_pypi_index_source_is_shippable(tmp_path: Path) -> None:
    """An explicit index entry pointing at PyPI is not a finding."""
    extra = (
        '[[tool.uv.index]]\nname = "pypi"\nurl = "https://pypi.org/simple"\n'
        '[tool.uv.sources]\ncherry = { index = "pypi" }\n'
    )
    assert _scan(tmp_path, extra) == []


def test_allowlist_downgrades_without_hiding(tmp_path: Path) -> None:
    """An allowed package still reports, carrying its reason, but stops blocking."""
    extra = "[tool.uv.sources]\ncherry = { workspace = true }\n"
    reason = "monorepo member, published separately"
    findings = _scan(tmp_path, extra, allow={"cherry": reason})
    assert len(findings) == 1
    assert not findings[0].blocking
    assert findings[0].allowed == reason
    assert reason in findings[0].verdict


def test_resolution_overrides_warn_without_blocking(tmp_path: Path) -> None:
    """`override-dependencies` diverges the tested tree without breaking installs."""
    extra = '[tool.uv]\noverride-dependencies = [ "mango>=2.5" ]\n'
    findings = _scan(tmp_path, extra)
    assert len(findings) == 1
    assert not findings[0].blocking


LOCK_WITH_GIT_SOURCE = """\
version = 1

[[package]]
name = "basket"
version = "1.0.0"
source = { editable = "." }

[[package]]
name = "melon"
version = "0.4.0"
source = { git = "https://x/melon?branch=main#abc123" }
"""


def test_lock_catches_what_pyproject_never_names(tmp_path: Path) -> None:
    """A transitively-pulled git source shows up even with a clean pyproject.

    This is why the gate reads the lock as well as the declarations: a source
    override can name a package no requirement array mentions, and only the
    resolved tree records it. The project's own `editable = "."` entry is not
    a dependency and is skipped.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(SHIPPABLE_PYPROJECT, encoding="UTF-8")
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_WITH_GIT_SOURCE, encoding="UTF-8")
    findings = scan_project(pyproject, lock, "1 week")
    assert [(f.package, f.kind, f.location) for f in findings] == [
        ("melon", SourceKind.GIT, "uv.lock")
    ]


def test_a_package_is_reported_once(tmp_path: Path) -> None:
    """A source flagged by both halves keeps the actionable pyproject finding."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        SHIPPABLE_PYPROJECT
        + '[tool.uv.sources]\nmelon = { git = "https://x/melon", branch = "main" }\n',
        encoding="UTF-8",
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_WITH_GIT_SOURCE, encoding="UTF-8")
    findings = scan_project(pyproject, lock, "1 week")
    assert [f.location for f in findings] == ["tool.uv.sources"]


@pytest.mark.parametrize(
    ("floor", "expected"),
    (
        # An unreleased floor is what the index cannot serve at all.
        ("mango>=2.1.0.dev0", "every install of this release fails"),
        # A released floor installs, just not the code that was tested.
        ("mango>=2", "not the code this release was built and tested against"),
    ),
)
def test_consequence_distinguishes_broken_from_divergent(
    tmp_path: Path,
    floor: str,
    expected: str,
) -> None:
    """The report separates a failing install from a silently different one."""
    doc = SHIPPABLE_PYPROJECT.replace('dependencies = [ "cherry>=1.2" ]', "").replace(
        '[dependency-groups]\ntest = [ "mango>=2" ]',
        f'dependencies = [ "{floor}" ]',
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        doc
        + '[tool.uv.sources]\nmango = { git = "https://x/mango", branch = "main" }\n',
        encoding="UTF-8",
    )
    findings = scan_project(pyproject, tmp_path / "uv.lock", "1 week")
    assert len(findings) == 1
    assert expected in findings[0].consequence


def test_release_readiness_flips_the_pr_opening(tmp_path: Path) -> None:
    """The release PR announces readiness only while the project has it."""
    pyproject = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"

    pyproject.write_text(SHIPPABLE_PYPROJECT, encoding="UTF-8")
    assert build_release_readiness(pyproject, lock, "1 week") == RELEASE_READY_SENTENCE

    pyproject.write_text(
        SHIPPABLE_PYPROJECT
        + '[tool.uv.sources]\ncherry = { git = "https://x/cherry", branch = "main" }\n',
        encoding="UTF-8",
    )
    banner = build_release_readiness(pyproject, lock, "1 week")
    assert banner.startswith("> [!CAUTION]")
    assert UNSHIPPABLE_BANNER_LEAD in banner
    # A verdict, not a report: the alert marker and one line naming the
    # package, with none of the prose or the table `lint-deps` renders.
    lines = [line for line in banner.strip().splitlines() if line.strip()]
    assert len(lines) == 2
    assert BLOCKER_SECTION_NOTE not in banner
    assert "| Package |" not in banner
    # Both lines stay quoted, or GitHub renders half of it outside the
    # admonition.
    assert all(line.startswith(">") for line in lines)

    # The package points at the line declaring it, linked when the caller
    # supplies a commit to hang the blob URL off, plain text otherwise.
    declared_line = next(
        number
        for number, line in enumerate(
            pyproject.read_text(encoding="UTF-8").splitlines(), start=1
        )
        if line.startswith("cherry =")
    )
    declaration = f"pyproject.toml#L{declared_line}"
    assert f"`cherry` ({declaration})" in banner
    linked = build_release_readiness(
        pyproject, lock, "1 week", source_url="https://x/repo/blob/deadbeef"
    )
    assert f"[`cherry`](https://x/repo/blob/deadbeef/{declaration})" in linked


def test_project_ships_only_released_dependencies() -> None:
    """This repository can be released as it stands.

    The generalization of `test_dependency_floors_clear_the_cooldown` above:
    that one covers a floor the cooldown makes unreachable, this one covers
    every other way a dependency fails to reach the people installing the
    published artifact. Both are release gates rather than CI symptoms, since
    this repository's own workflows install from `uv.lock` and resolve
    straight past the problem.

    The same check runs in the release lane (`_release-build.yaml`'s
    `lint-deps` job) and in the release PR body, so this is the local copy of
    a gate that also holds downstream. Its value here is latency: a `pytest`
    run says so in milliseconds, where CI takes minutes and the release PR
    banner needs a push.
    """
    findings = scan_project(
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "uv.lock",
        Config.minimum_release_age,
    )
    blocking = [finding for finding in findings if finding.blocking]
    assert not blocking, "\n".join(
        ["Unshippable dependencies would break this release:"]
        + [f"  {finding.message}" for finding in blocking]
    )
