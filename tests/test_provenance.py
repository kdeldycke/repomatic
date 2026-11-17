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
from email.message import Message
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

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
from repomatic.pypi import (
    PYPI_PACKAGE_URL,
    PYPI_PROVENANCE_URL,
    PYPI_SIMPLE_JSON,
    PYPI_SIMPLE_URL,
    PYPI_VERSION_API_URL,
    ReleaseProvenance,
    release_provenance,
)
from repomatic.tooling.registry import TOOL_REGISTRY
from tests.conftest import FakeResponse

TYPE_CHECKING = False
if TYPE_CHECKING:
    from urllib.request import Request

ATTESTED = ReleaseProvenance(
    known=True,
    files=2,
    attested_files=2,
    publisher="orchard/fig",
    workflow="release.yaml",
)
MOVED = ReleaseProvenance(
    known=True,
    files=2,
    attested_files=2,
    publisher="grove/fig",
    workflow="release.yaml",
)
RENAMED = ReleaseProvenance(
    known=True,
    files=2,
    attested_files=2,
    publisher="orchard/fig",
    workflow="publish.yaml",
)
PARTIAL = ReleaseProvenance(
    known=True,
    files=3,
    attested_files=2,
    publisher="orchard/fig",
    workflow="release.yaml",
)
BARE = ReleaseProvenance(known=True, files=2, attested_files=2)
NONE = ReleaseProvenance(known=True, files=2)
UNKNOWN = ReleaseProvenance(known=False)

PYPI = "https://pypi.org/simple"


# ---------------------------------------------------------------------------
# The PyPI lookup
# ---------------------------------------------------------------------------


RELEASE = {"fig-1.0-py3-none-any.whl": True, "fig-1.0.tar.gz": True}
"""The files of `fig 1.0`, each to whether it carries provenance."""

RELEASE_URL = PYPI_VERSION_API_URL.format(package="fig", version="1.0")
INDEX_URL = PYPI_SIMPLE_URL.format(package="fig")


def _provenance_body(repository: str) -> dict:
    """A provenance object naming one GitHub trusted publisher."""
    return {
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
    }


def _integrity_url(version: str, filename: str) -> str:
    """The integrity API URL of one file of `fig`."""
    return PYPI_PROVENANCE_URL.format(package="fig", version=version, filename=filename)


def _http_error(code: int) -> HTTPError:
    """An `HTTPError` with an empty body, as `urlopen` raises it."""
    return HTTPError("https://pypi.org", code, "Error", Message(), BytesIO())


def _pypi(releases, routes=None, calls=None):
    """A `urlopen` stand-in serving the releases of `fig` from PyPI.

    *releases* maps each version to its files, and each file to whether it
    carries provenance. The JSON API lists the files of each release, the
    Simple API gives each attested file its integrity URL, and the integrity
    API names `orchard/fig` as the publisher. *routes* replaces the answer to a
    URL with a JSON value, raw bytes, or an exception to raise. Any other URL
    answers 404. Each request is appended to *calls*, when given.
    """
    answers: dict[str, object] = {}
    index = []
    for version, files in releases.items():
        api_url = PYPI_VERSION_API_URL.format(package="fig", version=version)
        answers[api_url] = {"urls": [{"filename": name} for name in files]}
        for name, attested in files.items():
            url = _integrity_url(version, name)
            index.append({"filename": name, "provenance": url if attested else None})
            if attested:
                answers[url] = _provenance_body("orchard/fig")
    answers[INDEX_URL] = {"files": index}
    answers.update(routes or {})

    def urlopen(prepared, timeout):
        if calls is not None:
            calls.append(prepared)
        answer = answers.get(prepared.full_url, _http_error(404))
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(
            answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        )

    return urlopen


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(RELEASE, ATTESTED, id="full"),
        pytest.param(dict.fromkeys(RELEASE, False), NONE, id="none"),
        pytest.param(
            {**RELEASE, "fig-1.0-py313-none-any.whl": False}, PARTIAL, id="file-added"
        ),
    ],
)
def test_release_provenance_counts_every_file(files, expected):
    with patch("repomatic.http.urlopen", side_effect=_pypi({"1.0": files})):
        assert release_provenance("fig", "1.0") == expected


def test_release_provenance_asks_the_simple_api_for_json():
    """Without the JSON media type, PyPI answers HTML and every lookup is unknown."""
    calls: list[Request] = []
    with patch(
        "repomatic.http.urlopen", side_effect=_pypi({"1.0": RELEASE}, None, calls)
    ):
        release_provenance("fig", "1.0")

    (index,) = [call for call in calls if call.full_url == INDEX_URL]
    assert index.get_header("Accept") == PYPI_SIMPLE_JSON


def test_release_provenance_reads_the_publisher_of_an_attested_wheel():
    """The publisher comes from an attested file, a wheel before the sdist."""
    files = {
        "fig-1.0-py3-none-any.whl": False,
        "fig-1.0-py313-none-any.whl": True,
        "fig-1.0.tar.gz": True,
    }
    routes = {
        _integrity_url("1.0", "fig-1.0-py313-none-any.whl"): _provenance_body(
            "grove/fig"
        )
    }
    with patch("repomatic.http.urlopen", side_effect=_pypi({"1.0": files}, routes)):
        provenance = release_provenance("fig", "1.0")

    assert provenance.publisher == "grove/fig"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_http_error(404), id="missing"),
        pytest.param(_http_error(502), id="outage"),
        pytest.param(URLError("Connection reset"), id="network"),
        pytest.param(b"<html>", id="not-json"),
        pytest.param(b"[]", id="not-an-object"),
    ],
)
def test_release_provenance_keeps_the_counts_without_a_publisher(answer):
    """A publisher PyPI does not name leaves the counts of the Simple API."""
    routes = {_integrity_url("1.0", name): answer for name in RELEASE}
    with patch("repomatic.http.urlopen", side_effect=_pypi({"1.0": RELEASE}, routes)):
        assert release_provenance("fig", "1.0") == BARE


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param({RELEASE_URL: _http_error(502)}, id="release-outage"),
        pytest.param({RELEASE_URL: None}, id="release-null"),
        pytest.param({RELEASE_URL: {}}, id="release-without-urls"),
        pytest.param({RELEASE_URL: {"urls": []}}, id="release-without-files"),
        pytest.param({INDEX_URL: _http_error(502)}, id="index-outage"),
        pytest.param({INDEX_URL: b"<html>"}, id="index-not-json"),
        pytest.param(
            {
                INDEX_URL: {
                    "files": [{"filename": "fig-1.0.tar.gz", "provenance": None}]
                }
            },
            id="index-misses-a-file",
        ),
        pytest.param(
            {INDEX_URL: {"files": [{"filename": name} for name in RELEASE]}},
            id="index-without-provenance",
        ),
    ],
)
def test_release_provenance_is_unknown_when_pypi_does_not_answer(routes):
    """A failed lookup, or one that cannot place every file, proves nothing."""
    with patch("repomatic.http.urlopen", side_effect=_pypi({"1.0": RELEASE}, routes)):
        assert release_provenance("fig", "1.0") == UNKNOWN


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        pytest.param(NONE, ATTESTED, True, id="lost"),
        pytest.param(NONE, BARE, True, id="lost-unnamed"),
        pytest.param(PARTIAL, ATTESTED, True, id="file-added"),
        pytest.param(NONE, PARTIAL, True, id="lost-the-rest"),
        pytest.param(NONE, NONE, False, id="never-had-it"),
        pytest.param(ATTESTED, ATTESTED, False, id="kept"),
        pytest.param(PARTIAL, PARTIAL, False, id="partial-kept"),
        pytest.param(ATTESTED, PARTIAL, False, id="completed"),
        pytest.param(MOVED, ATTESTED, False, id="publisher-moved"),
        pytest.param(RENAMED, ATTESTED, False, id="workflow-renamed"),
        pytest.param(UNKNOWN, ATTESTED, False, id="new-unknown"),
        pytest.param(NONE, UNKNOWN, False, id="old-unknown"),
        pytest.param(NONE, None, False, id="added"),
        pytest.param(PARTIAL, None, False, id="added-partial"),
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
        pytest.param(
            RENAMED,
            ATTESTED,
            "⚠️ `orchard/fig` via `publish.yaml`, was `release.yaml`",
            id="renamed",
        ),
        pytest.param(
            MOVED, RENAMED, "⚠️ `grove/fig`, was `orchard/fig`", id="moved-and-renamed"
        ),
        pytest.param(PARTIAL, PARTIAL, "⚠️ 2 of 3 files attested", id="partial"),
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
        ("lime", "5.0"): ATTESTED,
        ("lime", "5.1"): PARTIAL,
        ("date", "6.0"): PARTIAL,
        ("date", "6.1"): PARTIAL,
        ("plum", "3.0"): NONE,
        ("plum", "3.1"): NONE,
        ("pear", "4.0"): NONE,
    }
    bumps = [
        PypiBump("fig", "fig", "1.0", "1.1"),
        PypiBump("kiwi", "kiwi", "2.0", "2.1"),
        PypiBump("lime", "lime", "5.0", "5.1"),
        PypiBump("date", "date", "6.0", "6.1"),
        PypiBump("plum", "plum", "3.0", "3.1"),
        PypiBump("pear", "pear", "", "4.0"),
    ]
    with patch(
        "repomatic.deps.provenance.release_provenance", side_effect=_lookup(table)
    ):
        review = review_bumps(bumps)

    assert review.holds == [
        ProvenanceHold("fig", "1.0", "1.1", "orchard/fig"),
        ProvenanceHold("lime", "5.0", "5.1", "orchard/fig"),
    ]
    assert review.held == {"fig", "lime"}
    assert review.cells == {
        "kiwi": "✅ `orchard/fig`",
        "date": "⚠️ 2 of 3 files attested",
        "plum": "none",
        "pear": "none",
    }


def test_review_holds_a_release_with_a_file_added_without_provenance():
    """A wheel uploaded beside the attested files of a release holds the bump.

    The other files of `fig 1.1` came through trusted publishing, and the JSON
    API lists one of them first: only a look at every file finds the wheel
    that a token added.
    """
    releases = {
        "1.0": {"fig-1.0-py3-none-any.whl": True, "fig-1.0.tar.gz": True},
        "1.1": {
            "fig-1.1-py3-none-any.whl": True,
            "fig-1.1-py313-none-any.whl": False,
            "fig-1.1.tar.gz": True,
        },
    }
    with patch("repomatic.http.urlopen", side_effect=_pypi(releases)):
        review = review_bumps([PypiBump("fig", "fig", "1.0", "1.1")])

    assert review.holds == [ProvenanceHold("fig", "1.0", "1.1", "orchard/fig")]
    assert review.cells == {}


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
