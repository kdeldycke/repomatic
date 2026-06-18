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

"""Tests for the `audit` command: read-only report by default, `--fix` to upgrade."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from repomatic import cli
from repomatic.cli import repomatic
from repomatic.config import Config
from repomatic.uv import AdvisorySource, VulnerablePackage


def _sample_vuln() -> VulnerablePackage:
    """Build a representative vulnerability advisory for the table tests."""
    return VulnerablePackage(
        name="aiohttp",
        current_version="3.14.0",
        advisory_id="GHSA-4fvr-rgm6-gqmc",
        advisory_title="Request smuggling",
        fixed_version="3.14.1",
        advisory_url="https://github.com/advisories/GHSA-4fvr-rgm6-gqmc",
        sources={AdvisorySource.UV_AUDIT},
    )


@pytest.fixture
def default_config(monkeypatch):
    """Pin the command to dataclass-default config, independent of the repo."""
    monkeypatch.setattr(cli, "get_tool_config", lambda ctx: Config())


@pytest.fixture
def no_github_repo(monkeypatch):
    """Drop GITHUB_REPOSITORY so --repo defaults to empty (CI sets it)."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


def _fail(*args, **kwargs):
    """Sentinel: a code path that must not run in the test under exercise."""
    raise AssertionError("unexpected call")


def test_audit_report_lists_vulnerabilities_and_exits_nonzero(
    monkeypatch, default_config, no_github_repo
):
    monkeypatch.setattr(
        cli, "collect_vulnerable_packages", lambda *a, **k: [_sample_vuln()]
    )
    result = CliRunner().invoke(
        repomatic,
        ["--no-color", "--table-format", "github", "audit"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "aiohttp" in result.output
    assert "GHSA-4fvr-rgm6-gqmc" in result.output


def test_audit_report_clean_exits_zero(monkeypatch, default_config, no_github_repo):
    monkeypatch.setattr(cli, "collect_vulnerable_packages", lambda *a, **k: [])
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "No known vulnerabilities found." in result.output


def test_audit_exit_zero_overrides_findings(
    monkeypatch, default_config, no_github_repo
):
    monkeypatch.setattr(
        cli, "collect_vulnerable_packages", lambda *a, **k: [_sample_vuln()]
    )
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--exit-zero"], catch_exceptions=False
    )
    assert result.exit_code == 0


def test_audit_report_does_not_call_the_fix_engine(
    monkeypatch, default_config, no_github_repo
):
    """Report mode is read-only: it must never reach the upgrade path."""
    monkeypatch.setattr(cli, "collect_vulnerable_packages", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_fix_vulnerable_deps", _fail)
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit"], catch_exceptions=False
    )
    assert result.exit_code == 0


def test_audit_output_writes_markdown(
    monkeypatch, default_config, no_github_repo, tmp_path
):
    monkeypatch.setattr(
        cli, "collect_vulnerable_packages", lambda *a, **k: [_sample_vuln()]
    )
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--output", str(out)], catch_exceptions=False
    )
    assert result.exit_code == 1
    content = out.read_text(encoding="UTF-8")
    assert "### Vulnerabilities" in content
    assert "aiohttp" in content


def test_audit_drops_github_source_without_repo(
    monkeypatch, default_config, no_github_repo
):
    captured = {}

    def fake_collect(lock_path, repo=None, sources=None):
        captured["repo"] = repo
        captured["sources"] = sources
        return []

    monkeypatch.setattr(cli, "collect_vulnerable_packages", fake_collect)
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert captured["repo"] is None
    assert AdvisorySource.GITHUB_ADVISORIES not in captured["sources"]
    assert AdvisorySource.UV_AUDIT in captured["sources"]


def test_audit_keeps_github_source_with_repo(monkeypatch, default_config):
    captured = {}

    def fake_collect(lock_path, repo=None, sources=None):
        captured["repo"] = repo
        captured["sources"] = sources
        return []

    monkeypatch.setattr(cli, "collect_vulnerable_packages", fake_collect)
    result = CliRunner().invoke(
        repomatic,
        ["--no-color", "audit", "--repo", "owner/name"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert captured["repo"] == "owner/name"
    assert AdvisorySource.GITHUB_ADVISORIES in captured["sources"]


def test_audit_fix_delegates_to_engine(monkeypatch, default_config, no_github_repo):
    calls = {}

    def fake_fix(lock_path, repo=None, sources=None):
        calls["fixed"] = True
        return True, "### Updated packages\n\n| pkg | old | new |"

    monkeypatch.setattr(cli, "_fix_vulnerable_deps", fake_fix)
    monkeypatch.setattr(cli, "collect_vulnerable_packages", _fail)
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--fix"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert calls.get("fixed")
    assert "Upgraded vulnerable packages." in result.output


def test_audit_fix_no_fixable_exits_zero(monkeypatch, default_config, no_github_repo):
    monkeypatch.setattr(cli, "_fix_vulnerable_deps", lambda *a, **k: (False, ""))
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--fix"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "No fixable vulnerabilities found." in result.output


def test_audit_fix_skipped_when_sync_disabled(monkeypatch, no_github_repo):
    config = Config()
    config.vulnerable_deps.sync = False
    monkeypatch.setattr(cli, "get_tool_config", lambda ctx: config)
    monkeypatch.setattr(cli, "_fix_vulnerable_deps", _fail)
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--fix"], catch_exceptions=False
    )
    assert result.exit_code == 0
