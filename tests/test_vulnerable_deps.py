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

"""Tests for vulnerability advisory collection (`uv audit` + GitHub Advisory DB)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repomatic.config import VulnerableDepsConfig
from repomatic.deps.vulnerable_deps import (
    AdvisorySource,
    VulnerablePackage,
    _run_uv_audit,
    _uv_version,
    collect_vulnerable_packages,
    fetch_dependabot_alerts,
    fix_vulnerable_deps,
    format_vulnerability_table,
    parse_uv_audit_json,
)

ALERTS_FIXTURE = [
    {
        "number": 6,
        "state": "open",
        "dependency": {
            "package": {"ecosystem": "pip", "name": "raspberry"},
            "manifest_path": "uv.lock",
            "scope": "runtime",
        },
        "security_advisory": {
            "ghsa_id": "GHSA-fruit-1111-aaaa",
            "summary": "Raspberry juice leak under concurrent picking",
            "html_url": "https://github.com/advisories/GHSA-fruit-1111-aaaa",
        },
        "security_vulnerability": {
            "package": {"ecosystem": "pip", "name": "raspberry"},
            "first_patched_version": {"identifier": "3.1.47"},
            "vulnerable_version_range": "< 3.1.47",
            "severity": "high",
        },
    },
    {
        "number": 7,
        "state": "open",
        "dependency": {
            "package": {"ecosystem": "pip", "name": "raspberry"},
            "manifest_path": "uv.lock",
            "scope": "runtime",
        },
        "security_advisory": {
            "ghsa_id": "GHSA-fruit-2222-bbbb",
            "summary": "Raspberry seed validation bypass",
            "html_url": "https://github.com/advisories/GHSA-fruit-2222-bbbb",
        },
        "security_vulnerability": {
            "package": {"ecosystem": "pip", "name": "raspberry"},
            "first_patched_version": {"identifier": "3.1.47"},
            "vulnerable_version_range": ">= 3.1.30, < 3.1.47",
            "severity": "high",
        },
    },
]


def test_config_default_sources_mirror_advisory_source_enum():
    """`VulnerableDepsConfig.sources` default must list every `AdvisorySource`.

    The default is spelled out as a literal in {mod}`repomatic.config` rather
    than derived from {class}`AdvisorySource`, because the module boundary
    forbids the derivation: `config` is a low-level module that
    `vulnerable_deps` imports, so `config` cannot import the enum back without
    a circular dependency. This test is the enforcement that keeps the literal
    in step with the enum's members and their declaration order.
    """
    assert VulnerableDepsConfig().sources == [source.value for source in AdvisorySource]


def test_fetch_dependabot_alerts_parses_each_entry():
    """Each alert maps to one VulnerablePackage tagged with GHSA source."""
    with patch(
        "repomatic.deps.vulnerable_deps.run_gh_command",
        return_value=json.dumps(ALERTS_FIXTURE),
    ):
        result = fetch_dependabot_alerts("orchard/raspberry")

    assert len(result) == 2
    for v in result:
        assert v.name == "raspberry"
        assert v.fixed_version == "3.1.47"
        assert v.sources == {AdvisorySource.GITHUB_ADVISORIES}
        assert v.advisory_url.startswith("https://github.com/advisories/GHSA-")


def test_fetch_dependabot_alerts_skips_entries_without_fix():
    """Alerts lacking first_patched_version are filtered out."""
    no_fix = [
        {
            "security_advisory": {"ghsa_id": "GHSA-cookbook-7777-zzzz", "summary": "x"},
            "security_vulnerability": {
                "package": {"name": "muffin", "ecosystem": "pip"},
                "first_patched_version": None,
            },
            "dependency": {"manifest_path": "uv.lock"},
        },
    ]
    with patch(
        "repomatic.deps.vulnerable_deps.run_gh_command",
        return_value=json.dumps(no_fix),
    ):
        assert fetch_dependabot_alerts("orchard/raspberry") == []


@pytest.mark.parametrize(
    "patch_kwargs",
    [
        pytest.param({"side_effect": RuntimeError("HTTP 403")}, id="api-error"),
        pytest.param({"return_value": "<<not json>>"}, id="invalid-json"),
    ],
)
def test_fetch_dependabot_alerts_degrades_to_empty(patch_kwargs):
    """Network/auth failures and unparsable responses degrade to an empty list."""
    with patch("repomatic.deps.vulnerable_deps.run_gh_command", **patch_kwargs):
        assert fetch_dependabot_alerts("orchard/raspberry") == []


@pytest.fixture
def lock_with_raspberry(tmp_path):
    """Create a uv.lock containing a single 'raspberry' package."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "raspberry"\nversion = "3.1.46"\n',
        encoding="UTF-8",
    )
    return lock


def test_collect_unions_uv_audit_and_ghsa(lock_with_raspberry):
    """Same package, different advisories: both kept, sources distinct."""
    audit_only = VulnerablePackage(
        name="raspberry",
        current_version="3.1.46",
        advisory_id="PYSEC-3333-cccc",
        advisory_title="Raspberry stem rot",
        fixed_version="3.1.47",
        advisory_url="https://example.com/PYSEC-3333-cccc",
        sources={AdvisorySource.UV_AUDIT},
    )
    with (
        patch(
            "repomatic.deps.vulnerable_deps._run_uv_audit", return_value=[audit_only]
        ),
        patch(
            "repomatic.deps.vulnerable_deps.run_gh_command",
            return_value=json.dumps(ALERTS_FIXTURE),
        ),
    ):
        merged = collect_vulnerable_packages(
            lock_with_raspberry, repo="orchard/raspberry"
        )

    advisory_ids = sorted(v.advisory_id for v in merged)
    assert advisory_ids == [
        "GHSA-fruit-1111-aaaa",
        "GHSA-fruit-2222-bbbb",
        "PYSEC-3333-cccc",
    ]
    # GHSA-only entries must have current_version backfilled from the lock.
    for v in merged:
        assert v.current_version == "3.1.46"


def test_collect_dedupes_when_advisory_id_matches(lock_with_raspberry):
    """Same package + same advisory ID across sources: merged into one entry."""
    same_advisory_audit = VulnerablePackage(
        name="raspberry",
        current_version="3.1.46",
        advisory_id="GHSA-fruit-1111-aaaa",
        advisory_title="",  # missing in audit, filled from GHSA
        fixed_version="3.1.47",
        advisory_url="",
        sources={AdvisorySource.UV_AUDIT},
    )
    with (
        patch(
            "repomatic.deps.vulnerable_deps._run_uv_audit",
            return_value=[same_advisory_audit],
        ),
        patch(
            "repomatic.deps.vulnerable_deps.run_gh_command",
            return_value=json.dumps(ALERTS_FIXTURE[:1]),
        ),
    ):
        merged = collect_vulnerable_packages(
            lock_with_raspberry, repo="orchard/raspberry"
        )

    assert len(merged) == 1
    only = merged[0]
    assert only.sources == {
        AdvisorySource.UV_AUDIT,
        AdvisorySource.GITHUB_ADVISORIES,
    }
    # Missing audit fields backfilled from the GHSA entry.
    assert only.advisory_title == "Raspberry juice leak under concurrent picking"
    assert only.advisory_url.endswith("GHSA-fruit-1111-aaaa")


def test_collect_skips_ghsa_when_repo_missing(lock_with_raspberry):
    """No repo argument means the GHSA source is skipped entirely."""
    with (
        patch("repomatic.deps.vulnerable_deps._run_uv_audit", return_value=[]),
        patch("repomatic.deps.vulnerable_deps.run_gh_command") as gh,
    ):
        result = collect_vulnerable_packages(lock_with_raspberry, repo=None)

    assert result == []
    gh.assert_not_called()


def test_collect_respects_sources_filter(lock_with_raspberry):
    """Only the explicitly requested sources are queried."""
    with (
        patch("repomatic.deps.vulnerable_deps._run_uv_audit") as audit,
        patch(
            "repomatic.deps.vulnerable_deps.run_gh_command",
            return_value=json.dumps(ALERTS_FIXTURE),
        ),
    ):
        result = collect_vulnerable_packages(
            lock_with_raspberry,
            repo="orchard/raspberry",
            sources=[AdvisorySource.GITHUB_ADVISORIES],
        )

    audit.assert_not_called()
    assert all(v.sources == {AdvisorySource.GITHUB_ADVISORIES} for v in result)


def test_collect_backfills_current_version_across_case_difference(tmp_path):
    """GHSA reports `GitPython`, lock stores `gitpython` — backfill must match."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "gitpython"\nversion = "3.1.46"\n',
        encoding="UTF-8",
    )
    alert = [
        {
            "security_advisory": {"ghsa_id": "GHSA-orchard-aaaa-bbbb", "summary": "x"},
            "security_vulnerability": {
                "package": {"name": "GitPython", "ecosystem": "pip"},
                "first_patched_version": {"identifier": "3.1.47"},
            },
            "dependency": {"manifest_path": "uv.lock"},
        },
    ]
    with (
        patch("repomatic.deps.vulnerable_deps._run_uv_audit", return_value=[]),
        patch(
            "repomatic.deps.vulnerable_deps.run_gh_command",
            return_value=json.dumps(alert),
        ),
    ):
        result = collect_vulnerable_packages(lock, repo="orchard/gitpython")

    assert len(result) == 1
    assert result[0].current_version == "3.1.46"


def test_format_vulnerability_table_includes_sources_column():
    """The rendered table credits each advisory's source(s)."""
    vulns = [
        VulnerablePackage(
            name="apricot",
            current_version="1.0",
            advisory_id="GHSA-orchard-9999-yyyy",
            advisory_title="Apricot pit cracking",
            fixed_version="1.1",
            advisory_url="https://example.com/orchard",
            sources={AdvisorySource.UV_AUDIT, AdvisorySource.GITHUB_ADVISORIES},
        ),
    ]
    table = format_vulnerability_table(vulns)
    assert "Sources" in table
    assert "`uv-audit`" in table
    assert "`github-advisories`" in table


def test_format_vulnerability_table_links_each_source_to_its_url():
    """Each source name in the Sources column links to its own advisory page."""
    vulns = [
        VulnerablePackage(
            name="apricot",
            current_version="1.0",
            advisory_id="GHSA-orchard-9999-yyyy",
            advisory_title="Apricot pit cracking",
            fixed_version="1.1",
            advisory_url="https://github.com/advisories/GHSA-orchard-9999-yyyy",
            sources={AdvisorySource.UV_AUDIT, AdvisorySource.GITHUB_ADVISORIES},
            source_urls={
                AdvisorySource.UV_AUDIT: "https://osv.dev/vulnerability/PYSEC",
                AdvisorySource.GITHUB_ADVISORIES: (
                    "https://github.com/advisories/GHSA-orchard-9999-yyyy"
                ),
            },
        ),
    ]
    table = format_vulnerability_table(vulns)
    assert "[`uv-audit`](https://osv.dev/vulnerability/PYSEC)" in table
    assert (
        "[`github-advisories`](https://github.com/advisories/GHSA-orchard-9999-yyyy)"
    ) in table


def test_collect_keeps_distinct_per_source_urls(lock_with_raspberry):
    """Merging same advisory from two sources retains both URLs."""
    audit = VulnerablePackage(
        name="raspberry",
        current_version="3.1.46",
        advisory_id="GHSA-fruit-1111-aaaa",
        advisory_title="",
        fixed_version="3.1.47",
        advisory_url="https://osv.dev/vulnerability/PYSEC-raspberry",
        sources={AdvisorySource.UV_AUDIT},
        source_urls={
            AdvisorySource.UV_AUDIT: "https://osv.dev/vulnerability/PYSEC-raspberry",
        },
    )
    with (
        patch("repomatic.deps.vulnerable_deps._run_uv_audit", return_value=[audit]),
        patch(
            "repomatic.deps.vulnerable_deps.run_gh_command",
            return_value=json.dumps(ALERTS_FIXTURE[:1]),
        ),
    ):
        merged = collect_vulnerable_packages(
            lock_with_raspberry, repo="orchard/raspberry"
        )

    assert len(merged) == 1
    only = merged[0]
    assert only.source_urls[AdvisorySource.UV_AUDIT].startswith("https://osv.dev/")
    assert only.source_urls[AdvisorySource.GITHUB_ADVISORIES].startswith(
        "https://github.com/advisories/"
    )


def test_parse_uv_audit_json_maps_fields():
    """A preview report maps onto VulnerablePackage, preferring display_id."""
    report = {
        "schema": {"version": "preview"},
        "summary": {
            "audited_packages": 10,
            "vulnerabilities": 1,
            "adverse_statuses": 0,
        },
        "vulnerabilities": [
            {
                "dependency": {"name": "raspberry", "version": "3.1.46"},
                "id": "PYSEC-2026-1",
                "display_id": "GHSA-fruit-1111-aaaa",
                "aliases": ["CVE-2026-0001", "PYSEC-2026-1"],
                "summary": "Raspberry juice leak under concurrent picking",
                "link": "https://osv.dev/vulnerability/PYSEC-2026-1",
                "fix_versions": ["3.1.47", "4.0.1"],
            },
        ],
        "adverse_statuses": [],
    }
    vulns = parse_uv_audit_json(json.dumps(report))

    assert vulns is not None
    assert len(vulns) == 1
    only = vulns[0]
    assert only.name == "raspberry"
    assert only.current_version == "3.1.46"
    # display_id wins over the OSV primary id for the human-facing identifier.
    assert only.advisory_id == "GHSA-fruit-1111-aaaa"
    assert only.advisory_title == "Raspberry juice leak under concurrent picking"
    # Every fix branch is preserved, not just the first.
    assert only.fixed_version == "3.1.47, 4.0.1"
    assert only.advisory_url.endswith("PYSEC-2026-1")
    # All other identifiers become aliases; the primary is excluded.
    assert only.aliases == {"CVE-2026-0001", "PYSEC-2026-1"}
    assert only.sources == {AdvisorySource.UV_AUDIT}


def test_parse_uv_audit_json_no_vulnerabilities():
    """A valid report with zero findings is an empty list, not a fallback."""
    report = {"schema": {"version": "preview"}, "vulnerabilities": []}
    assert parse_uv_audit_json(json.dumps(report)) == []


@pytest.mark.parametrize(
    "output",
    (
        pytest.param("", id="empty"),
        pytest.param("   \n  ", id="whitespace"),
        pytest.param("<<not json>>", id="malformed"),
        pytest.param("[1, 2, 3]", id="not-an-object"),
        pytest.param(json.dumps({"schema": {"version": "v2"}}), id="unknown-schema"),
        pytest.param(json.dumps({"vulnerabilities": []}), id="missing-schema"),
    ),
)
def test_parse_uv_audit_json_raises_on_unusable_output(output):
    """Unusable JSON raises so the scanner fails loud, never silently empty."""
    with pytest.raises(RuntimeError):
        parse_uv_audit_json(output)


def test_collect_dedupes_across_sources_via_alias(lock_with_raspberry):
    """A PYSEC from uv audit and its aliased GHSA from Dependabot merge."""
    audit = VulnerablePackage(
        name="raspberry",
        current_version="3.1.46",
        advisory_id="PYSEC-2026-1",
        advisory_title="Raspberry juice leak under concurrent picking",
        fixed_version="3.1.47",
        advisory_url="https://osv.dev/vulnerability/PYSEC-2026-1",
        aliases={"GHSA-fruit-1111-aaaa"},
        sources={AdvisorySource.UV_AUDIT},
    )
    with (
        patch("repomatic.deps.vulnerable_deps._run_uv_audit", return_value=[audit]),
        patch(
            "repomatic.deps.vulnerable_deps.run_gh_command",
            return_value=json.dumps(ALERTS_FIXTURE[:1]),  # GHSA-fruit-1111-aaaa
        ),
    ):
        merged = collect_vulnerable_packages(
            lock_with_raspberry, repo="orchard/raspberry"
        )

    assert len(merged) == 1
    only = merged[0]
    assert only.sources == {
        AdvisorySource.UV_AUDIT,
        AdvisorySource.GITHUB_ADVISORIES,
    }
    # uv is collected first, so its PYSEC stays primary and GHSA is an alias.
    assert only.advisory_id == "PYSEC-2026-1"
    assert "GHSA-fruit-1111-aaaa" in only.aliases


def test_fix_restores_the_lock_when_the_upgrade_moves_nothing(tmp_path):
    """An unreachable fix must leave `uv.lock` byte-identical.

    uv writes the `--exclude-newer-package` overrides it is handed into the
    lock's `[options]` table even when the resolution does not move, so a
    vulnerability capped out of reach by another dependency would otherwise
    dirty the file with a lone metadata line. That is enough for the
    `fix-vulnerable-deps` job to open a pull request carrying no fix and an
    empty report.
    """
    lock = tmp_path / "uv.lock"
    original = (
        "[options]\n"
        'exclude-newer-span = "P1W"\n'
        "\n"
        "[options.exclude-newer-package]\n"
        'papaya = "2026-08-11T00:00:00Z"\n'
        "\n"
        "[[package]]\n"
        'name = "raspberry"\n'
        'version = "3.1.46"\n'
    )
    lock.write_text(original, encoding="UTF-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv]\nexclude-newer = "1 week"\n', encoding="UTF-8"
    )

    vuln = VulnerablePackage(
        name="raspberry",
        current_version="3.1.46",
        advisory_id="GHSA-fruit-1111-aaaa",
        advisory_title="Raspberry juice leak under concurrent picking",
        fixed_version="3.1.47",
        advisory_url="https://github.com/advisories/GHSA-fruit-1111-aaaa",
        sources={AdvisorySource.UV_AUDIT},
    )

    def record_the_override(*args, **kwargs):
        """Stand in for `uv lock`: log the override, resolve the same version."""
        lock.write_text(
            original.replace(
                'papaya = "2026-08-11T00:00:00Z"\n',
                'papaya = "2026-08-11T00:00:00Z"\n'
                'raspberry = { timestamp = "0001-01-01T00:00:00Z", span = "PT0S" }\n',
            ),
            encoding="UTF-8",
        )

    with (
        patch(
            "repomatic.deps.vulnerable_deps.collect_vulnerable_packages",
            return_value=[vuln],
        ),
        patch(
            "repomatic.deps.vulnerable_deps.subprocess.run",
            side_effect=record_the_override,
        ),
    ):
        has_fixes, report = fix_vulnerable_deps(lock)

    assert has_fixes is False
    assert report == ""
    assert lock.read_text(encoding="UTF-8") == original


@pytest.mark.parametrize(
    ("stdout", "expected"),
    (
        pytest.param("uv 0.11.15 (abc1234 2026-05-18)\n", "0.11.15", id="with-hash"),
        pytest.param("uv 0.12.0\n", "0.12.0", id="bare"),
        pytest.param("uv 1.0.0 (deadbeef 2027-01-01)", "1.0.0", id="major"),
    ),
)
def test_uv_version_parses(stdout, expected):
    """`uv --version` output is parsed into a comparable Version."""
    completed = SimpleNamespace(stdout=stdout, stderr="")
    with patch("repomatic.deps.uv.subprocess.run", return_value=completed):
        assert str(_uv_version()) == expected


def test_run_uv_audit_parses_json(lock_with_raspberry):
    """A recent uv yields JSON that is parsed into records."""
    report = {
        "schema": {"version": "preview"},
        "vulnerabilities": [
            {
                "dependency": {"name": "raspberry", "version": "3.1.46"},
                "id": "PYSEC-2026-1",
                "display_id": "PYSEC-2026-1",
                "summary": "Raspberry juice leak",
                "link": "https://osv.dev/vulnerability/PYSEC-2026-1",
                "fix_versions": ["3.1.47"],
            },
        ],
    }
    version = SimpleNamespace(stdout="uv 0.11.15 (abc1234 2026-05-18)\n", stderr="")
    audit = SimpleNamespace(stdout=json.dumps(report), stderr="")
    with patch("repomatic.deps.uv.subprocess.run", side_effect=[version, audit]):
        vulns = _run_uv_audit(lock_with_raspberry)

    assert [v.advisory_id for v in vulns] == ["PYSEC-2026-1"]
    assert vulns[0].fixed_version == "3.1.47"


def test_run_uv_audit_rejects_old_uv(lock_with_raspberry):
    """An older uv (no JSON audit output) fails loud rather than scanning nothing."""
    version = SimpleNamespace(stdout="uv 0.11.14 (abc1234 2026-05-10)\n", stderr="")
    with (
        patch("repomatic.deps.uv.subprocess.run", return_value=version) as run,
        pytest.raises(RuntimeError, match="0.11.15"),
    ):
        _run_uv_audit(lock_with_raspberry)

    # Bails after the version check, never invoking `uv audit`.
    assert run.call_count == 1


def test_run_uv_audit_raises_on_unknown_schema(lock_with_raspberry):
    """A recent uv emitting an unrecognized schema version fails loud."""
    version = SimpleNamespace(stdout="uv 0.12.0 (abc1234 2026-06-01)\n", stderr="")
    audit = SimpleNamespace(stdout=json.dumps({"schema": {"version": "v2"}}), stderr="")
    with (
        patch("repomatic.deps.uv.subprocess.run", side_effect=[version, audit]),
        pytest.raises(RuntimeError, match="schema version"),
    ):
        _run_uv_audit(lock_with_raspberry)
