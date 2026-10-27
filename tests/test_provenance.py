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

"""Tests of the PEP 740 provenance lookup and of the review of a run's bumps."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import Mock, patch

import pytest

from repomatic.config import Config
from repomatic.deps.cooldown import SyncResult
from repomatic.deps.lockfile import LockFile
from repomatic.deps.pins import Candidate
from repomatic.deps.provenance import (
    PypiBump,
    is_regression,
    provenance_cell,
    review_bumps,
)
from repomatic.deps.report import (
    ProvenanceHold,
    format_diff_table,
    format_provenance_holds,
)
from repomatic.deps.sync import (
    ResolveContext,
    SyncPlan,
    _pinned_with_packages,
    _resolve_tool_versions,
    _review_lock_provenance,
)
from repomatic.github.releases import ReleaseNotesRequest
from repomatic.http import FetchError, Response
from repomatic.pypi import PYPI_PACKAGE_URL, ReleaseProvenance, release_provenance
from repomatic.tooling.registry import TOOL_REGISTRY

ATTESTED = ReleaseProvenance(known=True, attested=True, publisher="orchard/fig")
MOVED = ReleaseProvenance(known=True, attested=True, publisher="grove/fig")
BARE = ReleaseProvenance(known=True, attested=True)
NONE = ReleaseProvenance(known=True)
UNKNOWN = ReleaseProvenance(known=False)

PYPI = "https://pypi.org/simple"


# ---------------------------------------------------------------------------
# The PyPI lookup
# ---------------------------------------------------------------------------


RELEASE_FILES = {
    "urls": [
        {"filename": "fig-1.0.tar.gz"},
        {"filename": "fig-1.0-py3-none-any.whl"},
    ]
}


def _provenance_body(repository: str) -> bytes:
    """A provenance object naming one GitHub trusted publisher."""
    return json.dumps({
        "version": 1,
        "attestation_bundles": [
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": repository,
                    "workflow": "release.yaml",
                    "environment": None,
                },
                "attestations": [],
            }
        ],
    }).encode()


@patch("repomatic.pypi.get_json_soft", return_value=RELEASE_FILES)
def test_release_provenance_reads_the_publisher_of_the_wheel(mock_files):
    with patch(
        "repomatic.pypi.request",
        return_value=Response(200, {}, _provenance_body("orchard/fig")),
    ) as mock_request:
        provenance = release_provenance("fig", "1.0")

    assert provenance == ATTESTED
    assert mock_request.call_args[0][0].endswith(
        "/integrity/fig/1.0/fig-1.0-py3-none-any.whl/provenance"
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(FetchError("Not Found", status=404), NONE, id="none"),
        pytest.param(FetchError("Bad Gateway", status=502), UNKNOWN, id="outage"),
        pytest.param(FetchError("Connection reset"), UNKNOWN, id="network"),
    ],
)
@patch("repomatic.pypi.get_json_soft", return_value=RELEASE_FILES)
def test_release_provenance_tells_an_absence_from_a_failure(
    mock_files, error, expected
):
    """Only a 404 of the integrity API says the release carries no provenance."""
    with patch("repomatic.pypi.request", side_effect=error):
        assert release_provenance("fig", "1.0") == expected


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html>", id="not-json"),
        pytest.param(b"[]", id="not-an-object"),
    ],
)
@patch("repomatic.pypi.get_json_soft", return_value=RELEASE_FILES)
def test_release_provenance_of_an_unreadable_body_is_unknown(mock_files, body):
    """A body that is no JSON object proves no presence and no absence either."""
    with patch("repomatic.pypi.request", return_value=Response(200, {}, body)):
        assert release_provenance("fig", "1.0") == UNKNOWN


@pytest.mark.parametrize("files", [None, {}, {"urls": []}])
def test_release_provenance_without_files_is_unknown(files):
    with (
        patch("repomatic.pypi.get_json_soft", return_value=files),
        patch("repomatic.pypi.request") as mock_request,
    ):
        assert release_provenance("fig", "1.0") == UNKNOWN
    mock_request.assert_not_called()


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        pytest.param(NONE, ATTESTED, True, id="lost"),
        pytest.param(NONE, BARE, True, id="lost-unnamed"),
        pytest.param(NONE, NONE, False, id="never-had-it"),
        pytest.param(ATTESTED, ATTESTED, False, id="kept"),
        pytest.param(MOVED, ATTESTED, False, id="publisher-moved"),
        pytest.param(UNKNOWN, ATTESTED, False, id="new-unknown"),
        pytest.param(NONE, UNKNOWN, False, id="old-unknown"),
        pytest.param(NONE, None, False, id="added"),
    ],
)
def test_is_regression(new, old, expected):
    assert is_regression(new, old) is expected


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        pytest.param(ATTESTED, ATTESTED, "✅ `orchard/fig`", id="kept"),
        pytest.param(ATTESTED, None, "✅ `orchard/fig`", id="added"),
        pytest.param(BARE, ATTESTED, "✅ attested", id="unnamed"),
        pytest.param(MOVED, ATTESTED, "⚠️ `grove/fig`, was `orchard/fig`", id="moved"),
        pytest.param(NONE, NONE, "none", id="none"),
        pytest.param(UNKNOWN, ATTESTED, "_unknown_", id="unknown"),
    ],
)
def test_provenance_cell(new, old, expected):
    assert provenance_cell(new, old) == expected


def _lookup(table):
    """A `release_provenance` stand-in answering from *table*."""
    return lambda package, version: table[package, version]


def test_review_holds_the_bumps_that_lost_provenance():
    table = {
        ("fig", "1.0"): ATTESTED,
        ("fig", "1.1"): NONE,
        ("kiwi", "2.0"): ATTESTED,
        ("kiwi", "2.1"): ATTESTED,
        ("plum", "3.0"): NONE,
        ("plum", "3.1"): NONE,
        ("pear", "4.0"): NONE,
    }
    bumps = [
        PypiBump("fig", "fig", "1.0", "1.1"),
        PypiBump("kiwi", "kiwi", "2.0", "2.1"),
        PypiBump("plum", "plum", "3.0", "3.1"),
        PypiBump("pear", "pear", "", "4.0"),
    ]
    with patch(
        "repomatic.deps.provenance.release_provenance", side_effect=_lookup(table)
    ):
        review = review_bumps(bumps)

    assert review.holds == [ProvenanceHold("fig", "1.0", "1.1", "orchard/fig")]
    assert review.held == {"fig"}
    assert review.cells == {
        "kiwi": "✅ `orchard/fig`",
        "plum": "none",
        "pear": "none",
    }


# ---------------------------------------------------------------------------
# sync-tool-versions
# ---------------------------------------------------------------------------


def test_tool_versions_withhold_a_pypi_tool_that_lost_provenance(monkeypatch):
    """The registry keeps the pin of a tool whose new release lost provenance.

    A binary bump in the same run still lands, its cell naming the attestation
    the checksum recompute verifies.
    """

    yamllint = TOOL_REGISTRY["yamllint"]

    def candidates(spec):
        if spec.name == "yamllint":
            return [Candidate("99.0.0", "2025-12-01", "99.0.0")]
        if spec.name == "typos":
            return [Candidate("99.0.0", "2025-12-01", "v99.0.0")]
        return []

    table = {
        ("yamllint", yamllint.version): ATTESTED,
        ("yamllint", "99.0.0"): NONE,
    }
    monkeypatch.setattr("repomatic.deps.sync.tool_candidates", candidates)
    monkeypatch.setattr("repomatic.deps.pins.pypi_candidates", lambda _: [])
    monkeypatch.setattr("repomatic.deps.provenance.release_provenance", _lookup(table))

    plan = _resolve_tool_versions(
        ResolveContext(config=Config(), today=date(2026, 1, 1))
    )

    assert plan.provenance_holds == [
        ProvenanceHold("yamllint", yamllint.version, "99.0.0", "orchard/fig")
    ]
    assert [name for name, _old, _new in plan.changes] == ["typos"]
    content = next(iter(plan.file_writes.values()))
    assert f'version="{yamllint.version}"' in content
    assert plan.provenance["typos"] == "✅ immutable release attestation"


def test_tool_versions_bump_the_packages_pinned_beside_a_tool(monkeypatch):
    """A `with_packages` pin is reviewed and bumped like a tool of its own.

    The pin that lost provenance is held, and the others are rewritten. Each
    links to the repository PyPI names, else to its PyPI page. Only a package
    with a repository asks for release notes.
    """
    yamllint = TOOL_REGISTRY["yamllint"]
    assert yamllint.source_url is not None
    pins = _pinned_with_packages()
    (held, held_pin), (noted, noted_pin), (plain, plain_pin) = pins[:3]
    repository = "https://github.com/orchard/kiwi"
    release = [Candidate("99.0.0", "2025-12-01", "99.0.0")]

    table = {
        ("yamllint", yamllint.version): NONE,
        ("yamllint", "99.0.0"): NONE,
        (held, held_pin): ATTESTED,
        (held, "99.0.0"): NONE,
        (noted, noted_pin): ATTESTED,
        (noted, "99.0.0"): ATTESTED,
        (plain, plain_pin): NONE,
        (plain, "99.0.0"): NONE,
    }
    fetch = Mock(return_value={noted: (repository, [("v99.0.0", "Riper kiwis.")])})
    monkeypatch.setattr(
        "repomatic.deps.sync.tool_candidates",
        lambda spec: release if spec.name == "yamllint" else [],
    )
    monkeypatch.setattr(
        "repomatic.deps.pins.pypi_candidates",
        lambda package: release if package in (held, noted, plain) else [],
    )
    monkeypatch.setattr("repomatic.deps.provenance.release_provenance", _lookup(table))
    monkeypatch.setattr(
        "repomatic.deps.sync.get_source_url",
        lambda package: repository if package == noted else None,
    )
    monkeypatch.setattr("repomatic.deps.sync.fetch_github_release_notes", fetch)

    plan = _resolve_tool_versions(
        ResolveContext(config=Config(), today=date(2026, 1, 1), release_notes=True)
    )

    assert plan.provenance_holds == [
        ProvenanceHold(held, held_pin, "99.0.0", "orchard/fig")
    ]
    assert plan.changes == [
        ("yamllint", yamllint.version, "99.0.0"),
        (noted, noted_pin, "99.0.0"),
        (plain, plain_pin, "99.0.0"),
    ]
    # A tool with no binary has no attestation to verify, so its cell keeps the
    # verdict of the PyPI review.
    assert plan.provenance["yamllint"] == "none"
    content = next(iter(plan.file_writes.values()))
    assert f'"{held}=={held_pin}"' in content
    assert f'"{noted}==99.0.0"' in content
    assert f'"{plain}==99.0.0"' in content
    assert plan.name_urls[noted] == repository
    assert plan.name_urls[plain] == PYPI_PACKAGE_URL.format(package=plain)
    assert held not in plan.name_urls
    assert fetch.call_args.args[0] == [
        ReleaseNotesRequest(
            "yamllint",
            yamllint.source_url,
            yamllint.version,
            "99.0.0",
            yamllint.tag_pattern,
        ),
        ReleaseNotesRequest(noted, repository, noted_pin, "99.0.0"),
    ]
    assert "Riper kiwis." in plan.notes_section


# ---------------------------------------------------------------------------
# sync-uv-lock
# ---------------------------------------------------------------------------


def _lock(**versions: tuple[str, str | None]) -> LockFile:
    """A lock holding each package at a version, from PyPI or from git."""
    return LockFile({
        "package": [
            {
                "name": name,
                "version": version,
                "source": {"registry": PYPI} if registry else {"git": "x"},
            }
            for name, (version, registry) in versions.items()
        ]
    })


def _lock_text(**versions: tuple[str, str | None]) -> str:
    """The TOML of {func}`_lock`, as `uv lock` writes it."""
    lines = ["version = 1", ""]
    for name, (version, registry) in versions.items():
        source = f'{{ registry = "{PYPI}" }}' if registry else '{ git = "x" }'
        lines += [
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            f"source = {source}",
            "",
        ]
    return "\n".join(lines)


def test_uv_lock_steps_back_a_package_that_lost_provenance(tmp_path, monkeypatch):
    """The lost bump is locked again at its previous version; the others stay."""
    lockfile = tmp_path / "uv.lock"
    upgraded = {"fig": ("1.1", PYPI), "kiwi": ("2.1", PYPI), "plum": ("3.1", None)}
    lockfile.write_text(_lock_text(**upgraded), encoding="UTF-8")
    result = SyncResult(
        changes=[("fig", "1.0", "1.1"), ("kiwi", "2.0", "2.1"), ("plum", "3.0", "3.1")],
        lock=_lock(**upgraded),
    )
    table = {
        ("fig", "1.0"): ATTESTED,
        ("fig", "1.1"): NONE,
        ("kiwi", "2.0"): ATTESTED,
        ("kiwi", "2.1"): ATTESTED,
    }
    monkeypatch.setattr("repomatic.deps.provenance.release_provenance", _lookup(table))
    settings = Mock()
    settings.lock_command.side_effect = lambda *args: ["uv", "lock", *args]

    def relock(cmd, cwd):
        held = {"fig": ("1.0", PYPI), "kiwi": ("2.1", PYPI), "plum": ("3.1", None)}
        lockfile.write_text(_lock_text(**held), encoding="UTF-8")

    plan = SyncPlan(operation="sync-uv-lock", subject="Package", heading="Packages")
    rc = ResolveContext(config=Config(), today=date(2026, 1, 1), lockfile=lockfile)
    with patch("repomatic.deps.sync.run_uv", side_effect=relock) as mock_run:
        changes, final = _review_lock_provenance(plan, rc, settings, result)

    assert mock_run.call_args[0][0] == ["uv", "lock", "--upgrade-package", "fig==1.0"]
    assert changes == [("kiwi", "2.0", "2.1"), ("plum", "3.0", "3.1")]
    assert final.versions["fig"] == "1.0"
    assert plan.provenance_holds == [ProvenanceHold("fig", "1.0", "1.1", "orchard/fig")]
    # The git-sourced package is never looked up: it comes from no index.
    assert plan.provenance == {"kiwi": "✅ `orchard/fig`"}


def test_uv_lock_without_a_regression_keeps_the_upgrade(tmp_path, monkeypatch):
    upgraded = {"kiwi": ("2.1", PYPI)}
    result = SyncResult(changes=[("kiwi", "2.0", "2.1")], lock=_lock(**upgraded))
    table = {("kiwi", "2.0"): ATTESTED, ("kiwi", "2.1"): ATTESTED}
    monkeypatch.setattr("repomatic.deps.provenance.release_provenance", _lookup(table))
    plan = SyncPlan(operation="sync-uv-lock", subject="Package", heading="Packages")
    rc = ResolveContext(config=Config(), today=date(2026, 1, 1))
    with patch("repomatic.deps.sync.run_uv") as mock_run:
        changes, final = _review_lock_provenance(plan, rc, Mock(), result)

    mock_run.assert_not_called()
    assert changes == result.changes
    assert final is result.lock
    assert plan.provenance_holds == []


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_diff_table_gains_a_provenance_column():
    table = format_diff_table(
        [("fig", "1.0", "1.1"), ("kiwi", "2.0", "2.1")],
        upload_times={
            "fig": "2026-01-01T00:00:00Z",
            "kiwi": "2026-01-02T00:00:00Z",
        },
        provenance={"fig": "✅ `orchard/fig`"},
    )
    # Provenance sits right after the change, and dates close the row.
    assert "| Package | Change | Provenance | Released |" in table
    assert "| fig | `1.0` → `1.1` | ✅ `orchard/fig` | 2026-01-01 |" in table
    # A changed name without a cell shows a dash, not an empty cell.
    assert "| kiwi | `2.0` → `2.1` | — | 2026-01-02 |" in table


def test_diff_table_without_provenance_keeps_its_columns():
    table = format_diff_table([("fig", "1.0", "1.1")], provenance={"pear": "none"})
    assert "Provenance" not in table


def test_provenance_holds_section():
    section = format_provenance_holds(
        [ProvenanceHold("fig", "1.0", "1.1", "orchard/fig")],
        name_urls={"fig": "https://pypi.org/project/fig/"},
    )
    assert section.startswith("## 🛡️ Held back for missing provenance")
    assert "[fig](https://pypi.org/project/fig/)" in section
    assert "`1.0`" in section and "`1.1`" in section and "`orchard/fig`" in section
    assert format_provenance_holds([]) == ""
