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

"""Tests for the `audit` command (read-only report by default, `--fix` to
upgrade) and the `exclude-newer-package` cooldown-bypass lifecycle."""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from repomatic import cli
from repomatic.cli import repomatic
from repomatic.config import Config
from repomatic.uv import (
    BYPASS_NEEDS_RELEASE,
    BypassForecast,
    HeldBackPackage,
    build_held_back,
    compute_bypass_forecasts,
    compute_pruned_forecasts,
    format_bypass_section,
    format_diff_table,
    format_exclude_newer_note,
    format_held_back_table,
    format_release_notes,
    freeze_exclude_newer_packages,
    prune_stale_exclude_newer_packages,
    pypi_name_urls,
)
from repomatic.vulnerable_deps import (
    AdvisorySource,
    VulnerablePackage,
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
    assert "## Vulnerabilities" in content
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
        return True, "## Updated packages\n\n| pkg | old | new |"

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
    assert "## 🆙 Updated tools" in table
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
    assert "## 🆙 Updated packages" in pkg
    assert "| Package | Change |" in pkg
    assert "[ruff](https://pypi.org/project/ruff/)" in pkg
    assert "`0.1.0` → `0.2.0`" in pkg

    # A name absent from name_urls renders plain, and no changes yields nothing.
    assert "| foo |" in format_diff_table([("foo", "1", "2")])
    assert format_diff_table([]) == ""

    # Added and removed packages share one label layout: emoji status first,
    # then the version.
    assert "| papaya | 🆕 new: `1.4.0` |" in format_diff_table([
        ("papaya", "", "1.4.0"),
    ])
    assert "| papaya | 🗑️ removed: `0.7.0` |" in format_diff_table([
        ("papaya", "0.7.0", ""),
    ])


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


def test_format_diff_table_released_overrides():
    """An override replaces the date cell and marks the row as an exception."""
    # The override wins over a date for its row; other rows keep their dates.
    table = format_diff_table(
        [("mango", "1.0.0", "2.0.0"), ("papaya", "3.0.0", "4.0.0")],
        upload_times={"mango": "2026-06-20", "papaya": "2026-06-21"},
        released_overrides={"papaya": "⛓️ EXEMPT"},
    )
    assert "| mango | `1.0.0` → `2.0.0` | 2026-06-20 |" in table
    assert "| papaya | `3.0.0` → `4.0.0` | ⛓️ EXEMPT |" in table

    # An override on a changed row forces the column on without any dates,
    # and rows without a date or override render an empty cell.
    table = format_diff_table(
        [("mango", "1.0.0", "2.0.0"), ("papaya", "3.0.0", "4.0.0")],
        released_overrides={"papaya": "⛓️ EXEMPT"},
    )
    assert "| Package | Change | Released |" in table
    assert "| mango | `1.0.0` → `2.0.0` |  |" in table
    assert "| papaya | `3.0.0` → `4.0.0` | ⛓️ EXEMPT |" in table

    # An override naming no changed row is ignored and leaves the column off.
    table = format_diff_table(
        [("mango", "1.0.0", "2.0.0")],
        released_overrides={"papaya": "⛓️ EXEMPT"},
    )
    assert "| Package | Change |" in table
    assert "Released" not in table


def test_format_diff_table_call_sites_pass_reference_date():
    """Every PR-report call site opts into the humanized `Released` delta.

    `reference_date` drives the relative hint on the `Released` column; a call
    site omitting it renders bare dates, as `fix-vulnerable-deps` once did
    while the other dependency updaters showed the delta.
    """
    package_dir = Path(cli.__file__).parent
    violations = []
    for py_file in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="UTF-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else "")
            )
            if name != "format_diff_table":
                continue
            if "reference_date" not in {kw.arg for kw in node.keywords}:
                violations.append(f"{py_file.name}:{node.lineno}")
    assert not violations, (
        f"format_diff_table calls missing reference_date: {violations}"
    )


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
    rows = [
        HeldBackPackage(
            "actions/checkout", "6.0.3", "7.0.0", "2026-06-26", "2026-07-04"
        )
    ]
    table = format_held_back_table(
        rows,
        "CUSTOM COOLDOWN NOTE",
        name_urls={"actions/checkout": "https://github.com/actions/checkout"},
        subject="Action",
    )
    assert "## ⏸️ Held back by cooldown" in table
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


def test_format_release_notes_nests_headings():
    """The h3 section holds h4 version headings, with upstream bodies below.

    The section heading is an h3 so it nests under the PR body's h2 update
    table, and the embedded upstream body's own headings are demoted below
    the h4 version heading instead of colliding with the PR's h2 sections.
    """
    notes = {
        "papaya": (
            "https://github.com/orchard/papaya",
            [("v2.0.0", "## Flavor\n\nSweeter pulp.\n\n### Texture\n\nFirmer.")],
        ),
    }
    section = format_release_notes(notes)
    assert section.startswith("### Release notes\n")
    assert "<summary><code>papaya</code></summary>" in section
    assert (
        "#### [`v2.0.0`](https://github.com/orchard/papaya/releases/tag/v2.0.0)"
        in section
    )
    assert "##### Flavor" in section
    assert "###### Texture" in section
    assert "\n## Flavor" not in section
    assert format_release_notes({}) == ""


def _write_pyproject(tmp_path: Path, uv_lines: str) -> Path:
    """Write a minimal `pyproject.toml` with the given `[tool.uv]` body."""
    path = tmp_path / "pyproject.toml"
    path.write_text(f"[tool.uv]\n{uv_lines}", encoding="UTF-8")
    return path


def _write_lock(tmp_path: Path, *packages: tuple[str, str, str]) -> Path:
    """Write a minimal `uv.lock` with a `P1W` cooldown span.

    Each package is a `(name, version, upload_time)` triple; an empty upload
    time omits the `sdist` block, mimicking a git or path source.
    """
    lines = [
        "version = 1",
        'requires-python = ">=3.10"',
        "",
        "[options]",
        'exclude-newer = "0001-01-01T00:00:00Z"',
        'exclude-newer-span = "P1W"',
    ]
    for name, version, upload in packages:
        lines.extend([
            "",
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            'source = { registry = "https://pypi.org/simple" }',
        ])
        if upload:
            lines.append(
                f'sdist = {{ url = "https://example.test/{name}.tar.gz",'
                f' upload-time = "{upload}" }}'
            )
    path = tmp_path / "uv.lock"
    path.write_text("\n".join(lines) + "\n", encoding="UTF-8")
    return path


def test_prune_stale_exclude_newer_packages_returns_pruned_names(tmp_path):
    """An entry whose held version aged past the cutoff is dropped by name."""
    pyproject = _write_pyproject(
        tmp_path,
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { mango = "2026-01-02T00:00:00Z" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-01-01T12:00:00Z"))
    assert prune_stale_exclude_newer_packages(pyproject, lock) == {"mango"}
    assert "exclude-newer-package" not in pyproject.read_text(encoding="UTF-8")


def test_prune_stale_exclude_newer_packages_keeps_active_freeze(tmp_path):
    """A freeze whose held version is still inside the window stays put."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pyproject = _write_pyproject(
        tmp_path,
        f'exclude-newer = "1 week"\nexclude-newer-package = {{ mango = "{fresh}" }}\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", fresh))
    before = pyproject.read_text(encoding="UTF-8")
    assert prune_stale_exclude_newer_packages(pyproject, lock) == set()
    assert pyproject.read_text(encoding="UTF-8") == before


def test_freeze_exclude_newer_packages_returns_frozen_names(tmp_path):
    """A relative-span bypass is rewritten to hold the locked version."""
    pyproject = _write_pyproject(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = "0 day" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-01T12:00:00Z"))
    assert freeze_exclude_newer_packages(pyproject, lock) == {"mango"}
    # Frozen just past the held upload: the day after (upload + 1 day).
    assert (
        'exclude-newer-package = { mango = "2026-07-03T00:00:00Z" }'
        in pyproject.read_text(encoding="UTF-8")
    )
    # Re-running is a no-op: the frozen cutoff already holds.
    assert freeze_exclude_newer_packages(pyproject, lock) == set()


def test_freeze_exclude_newer_packages_pins_bare_date(tmp_path):
    """A legacy bare date is pinned to the equivalent explicit UTC instant."""
    pyproject = _write_pyproject(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = "2026-06-13" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-06-12T08:00:00Z"))
    assert freeze_exclude_newer_packages(pyproject, lock) == {"mango"}
    assert '"2026-06-14T00:00:00Z"' in pyproject.read_text(encoding="UTF-8")


def test_freeze_exclude_newer_packages_keeps_span_without_upload_time(tmp_path):
    """A git/path package has no release to freeze against: span kept as-is."""
    pyproject = _write_pyproject(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { papaya = "0 day" }\n',
    )
    lock = _write_lock(tmp_path, ("papaya", "1.0.0", ""))
    before = pyproject.read_text(encoding="UTF-8")
    assert freeze_exclude_newer_packages(pyproject, lock) == set()
    assert pyproject.read_text(encoding="UTF-8") == before


def test_compute_bypass_forecasts_reports_freezes_only(tmp_path):
    """Fixed-timestamp freezes get an expiry; spans and dropped deps do not."""
    pyproject = _write_pyproject(
        tmp_path,
        'exclude-newer = "1 week"\n'
        "exclude-newer-package = {"
        ' cherry = "2026-01-02T00:00:00Z",'
        ' mango = "2026-07-02T00:00:00Z",'
        ' papaya = "0 day" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-01T12:00:00Z"))
    forecasts = compute_bypass_forecasts(pyproject, lock)
    # papaya is a permanent span and cherry left the lock: only mango shows.
    assert len(forecasts) == 1
    assert forecasts[0].name == "mango"
    assert forecasts[0].held_version == "2.0.0"
    # The held upload (2026-07-01) plus the lock's P1W span.
    assert forecasts[0].expires.startswith("2026-07-08")


def test_compute_bypass_forecasts_flags_unreleased_hold(tmp_path):
    """A freeze holding a version with no upload time can only end by release."""
    pyproject = _write_pyproject(
        tmp_path,
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { papaya = "2026-07-02T00:00:00Z" }\n',
    )
    lock = _write_lock(tmp_path, ("papaya", "1.0.0.dev0", ""))
    forecasts = compute_bypass_forecasts(pyproject, lock)
    assert forecasts == [BypassForecast("papaya", "1.0.0.dev0", BYPASS_NEEDS_RELEASE)]


def test_compute_bypass_forecasts_without_entries(tmp_path):
    """No `exclude-newer-package` table yields no forecasts."""
    pyproject = _write_pyproject(tmp_path, 'exclude-newer = "1 week"\n')
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-01T12:00:00Z"))
    assert compute_bypass_forecasts(pyproject, lock) == []


def test_compute_pruned_forecasts_snapshots_cleared_freezes(tmp_path):
    """Pruned entries keep the version and the (past) date the freeze aged out."""
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-01-01T12:00:00Z"))
    records = compute_pruned_forecasts({"mango"}, lock)
    assert len(records) == 1
    assert records[0].name == "mango"
    assert records[0].held_version == "2.0.0"
    # The held upload (2026-01-01) plus the lock's P1W span, long past.
    assert records[0].expires.startswith("2026-01-08")
    assert compute_pruned_forecasts(set(), lock) == []


def test_format_bypass_section_renders_unified_table():
    """Every lifecycle state is a row: active plain, frozen and cleared labelled."""
    section = format_bypass_section(
        [
            BypassForecast("mango", "2.0.0", "2026-07-08 (in 2 days)"),
            BypassForecast("papaya", "3.1.0", "2026-07-10 (in 4 days)"),
        ],
        pruned=[BypassForecast("cherry", "1.5.0", "2026-07-01 (5 days ago)")],
        frozen=["papaya"],
        name_urls=pypi_name_urls([
            ("cherry", "", ""),
            ("mango", "", ""),
            ("papaya", "", ""),
        ]),
    )
    assert "## ❄️ Cooldown bypasses" in section
    assert "| Package | Held at | Held until |" in section
    assert (
        "| [cherry](https://pypi.org/project/cherry/) | 🧹 cleared: `1.5.0` |"
        " 2026-07-01 (5 days ago) |"
    ) in section
    assert (
        "| [mango](https://pypi.org/project/mango/) | `2.0.0` |"
        " 2026-07-08 (in 2 days) |"
    ) in section
    assert (
        "| [papaya](https://pypi.org/project/papaya/) | 📌 frozen: `3.1.0` |"
        " 2026-07-10 (in 4 days) |"
    ) in section
    # Rows merge sorted by name, and the old prose lines are gone.
    assert section.index("cherry") < section.index("mango") < section.index("papaya")
    assert "in this PR" not in section
    # Empty input yields nothing; an unmapped name renders plain.
    assert format_bypass_section([]) == ""
    assert "| mango |" in format_bypass_section([BypassForecast("mango", "1", "")])


def test_format_bypass_section_labels_unreleased_hold():
    """An unreleased hold is labelled, with an italicized expiry marker."""
    section = format_bypass_section([
        BypassForecast("papaya", "1.0.0.dev0", BYPASS_NEEDS_RELEASE)
    ])
    assert (f"| 🚧 unreleased: `1.0.0.dev0` | *{BYPASS_NEEDS_RELEASE}* |") in section
