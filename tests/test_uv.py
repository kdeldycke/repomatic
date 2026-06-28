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

from datetime import date, timedelta

import pytest
from click.testing import CliRunner

from repomatic import cli
from repomatic.cli import repomatic
from repomatic.config import Config
from repomatic.uv import (
    AdvisorySource,
    HeldBackPackage,
    VulnerablePackage,
    build_held_back,
    format_diff_table,
    format_exclude_newer_note,
    format_held_back_table,
    pypi_name_urls,
)


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


def test_format_diff_table_is_the_shared_pr_table():
    """One formatter renders every dependency PR table identically.

    `sync-uv-lock`, `fix-vulnerable-deps`, `sync-tool-versions`,
    `sync-action-pins`, and `sync-workflow-pins` all route through this, so the
    heading, first-column subject, name link, and change link are all
    parametrized rather than hard-coded.
    """
    # A sync-tool-versions / sync-action-pins style call: GitHub name link plus
    # a comparison link on the change cell, with a custom heading and subject.
    table = format_diff_table(
        [("gitleaks", "8.30.1", "8.31.0")],
        upload_times={"gitleaks": "2026-06-20"},
        name_urls={"gitleaks": "https://github.com/gitleaks/gitleaks"},
        comparison_urls={
            "gitleaks": "https://github.com/gitleaks/gitleaks/compare/v8.30.1...v8.31.0"
        },
        heading="Updated tools",
        subject="Tool",
    )
    assert "### 🆙 Updated tools" in table
    assert "| Tool | Change | Released |" in table
    assert "[gitleaks](https://github.com/gitleaks/gitleaks)" in table
    assert (
        "[`8.30.1` → `8.31.0`]"
        "(https://github.com/gitleaks/gitleaks/compare/v8.30.1...v8.31.0)"
    ) in table

    # The PyPI defaults (sync-uv-lock / fix-vulnerable-deps): "Updated packages"
    # heading, PyPI name links via the helper, plain change with no comparison.
    pkg = format_diff_table(
        [("ruff", "0.1.0", "0.2.0")],
        name_urls=pypi_name_urls([("ruff", "0.1.0", "0.2.0")]),
    )
    assert "### 🆙 Updated packages" in pkg
    assert "| Package | Change |" in pkg
    assert "[ruff](https://pypi.org/project/ruff/)" in pkg
    assert "`0.1.0` → `0.2.0`" in pkg

    # A name absent from name_urls renders plain, and no changes yields nothing.
    assert "| foo |" in format_diff_table([("foo", "1", "2")])
    assert format_diff_table([]) == ""


def test_format_exclude_newer_note():
    """The uv cutoff sentence renders the lock's exclude-newer date, or nothing."""
    note = format_exclude_newer_note("2026-06-21T00:00:00Z")
    assert note == (
        "Resolved with [`exclude-newer`]"
        "(https://docs.astral.sh/uv/reference/settings/#exclude-newer)"
        " cutoff: `2026-06-21`."
    )
    assert format_exclude_newer_note("") == ""


def test_format_diff_table_shows_cooldown_note_above_table():
    """A non-empty cooldown_note is rendered between the heading and the table."""
    table = format_diff_table([("ruff", "0.1.0", "0.2.0")], cooldown_note="MY NOTE")
    assert table.index("MY NOTE") < table.index("| Package | Change |")
    # An empty note leaves no stray paragraph.
    assert "MY NOTE" not in format_diff_table([("ruff", "0.1.0", "0.2.0")])


def test_build_held_back_formats_released_and_eligible():
    """build_held_back turns raw selection data into the uv-style row strings."""
    today = date(2026, 6, 28)
    row = build_held_back(
        "astral-sh/setup-uv", "8.2.0", "8.3.0", "2026-06-26", timedelta(days=8), today
    )
    assert row == HeldBackPackage(
        name="astral-sh/setup-uv",
        locked_version="8.2.0",
        available_version="8.3.0",
        released="2026-06-26 (2 days ago)",
        eligible="2026-07-04 (in 6 days)",
    )


def test_format_held_back_table_is_parametrized_by_subject_and_links():
    """One renderer serves uv (PyPI) and the version-sync updaters (GitHub/npm)."""
    rows = [HeldBackPackage("actions/checkout", "6.0.3",
                            "7.0.0", "2026-06-26", "2026-07-04")]
    table = format_held_back_table(
        rows,
        "CUSTOM COOLDOWN NOTE",
        name_urls={"actions/checkout": "https://github.com/actions/checkout"},
        subject="Action",
    )
    assert "### 🔜 Held back by cooldown" in table
    assert "CUSTOM COOLDOWN NOTE" in table
    assert "| Action | Locked | Available | Released | Eligible |" in table
    assert "[actions/checkout](https://github.com/actions/checkout)" in table
    assert "| `6.0.3` | `7.0.0` |" in table
    # Empty input yields nothing; an unmapped name renders plain.
    assert format_held_back_table([]) == ""
    plain = format_held_back_table(
        [HeldBackPackage("x", "1", "2", "", "")], "n", subject="Tool"
    )
    assert "| x | `1` | `2` |" in plain
