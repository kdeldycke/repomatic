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
upgrade) and the `repomatic.uv` lock helpers behind the
`exclude-newer-package` cooldown-bypass lifecycle.

The `repomatic.dep_report` renderers these helpers feed are covered by
`tests/test_dep_report.py`'s golden renders.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import tomlrt
from click.testing import CliRunner

from repomatic import cli
from repomatic.cli import repomatic
from repomatic.config import Config
from repomatic.dep_report import BYPASS_NEEDS_RELEASE, BypassForecast
from repomatic.uv import (
    compute_bypass_forecasts,
    compute_pruned_forecasts,
    freeze_exclude_newer_packages,
    parse_lock_specifiers,
    project_exclude_newer,
    prune_stale_exclude_newer_packages,
)
from repomatic.vulnerable_deps import (
    AdvisorySource,
    VulnerablePackage,
)

REPO_ROOT = Path(__file__).parent.parent


def test_cooldown_windows_match_minimum_release_age() -> None:
    """The lock window and the install window are the same duration.

    `[tool.uv] exclude-newer` gates `uv lock`, while `[tool.repomatic]
    minimum-release-age` gates the `uvx` installs every workflow runs. A lock
    window wider than the install window resolves versions those installs then
    refuse, leaving a package pinned in `uv.lock` that CI cannot install.
    Keeping the two literals equal closes that band.
    """
    window = Config.minimum_release_age

    project = project_exclude_newer(REPO_ROOT / "pyproject.toml")
    assert project == window, (
        f"[tool.uv] exclude-newer is {project!r}, expected {window!r} to match "
        "[tool.repomatic] minimum-release-age."
    )

    bundled_path = REPO_ROOT / "repomatic" / "data" / "uv.toml"
    bundled = tomlrt.loads(bundled_path.read_text(encoding="UTF-8"))
    assert bundled.get("exclude-newer") == window, (
        f"{bundled_path.name} exclude-newer is "
        f"{bundled.get('exclude-newer')!r}, expected {window!r}."
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


def _write_uv_config(tmp_path: Path, uv_lines: str) -> Path:
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
    pyproject = _write_uv_config(
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
    pyproject = _write_uv_config(
        tmp_path,
        f'exclude-newer = "1 week"\nexclude-newer-package = {{ mango = "{fresh}" }}\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", fresh))
    before = pyproject.read_text(encoding="UTF-8")
    assert prune_stale_exclude_newer_packages(pyproject, lock) == set()
    assert pyproject.read_text(encoding="UTF-8") == before


def test_freeze_exclude_newer_packages_returns_frozen_names(tmp_path):
    """A relative-span bypass is rewritten to hold the locked version."""
    pyproject = _write_uv_config(
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
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = "2026-06-13" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-06-12T08:00:00Z"))
    assert freeze_exclude_newer_packages(pyproject, lock) == {"mango"}
    assert '"2026-06-14T00:00:00Z"' in pyproject.read_text(encoding="UTF-8")


def test_freeze_exclude_newer_packages_keeps_span_without_upload_time(tmp_path):
    """A git/path package has no release to freeze against: span kept as-is."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { papaya = "0 day" }\n',
    )
    lock = _write_lock(tmp_path, ("papaya", "1.0.0", ""))
    before = pyproject.read_text(encoding="UTF-8")
    assert freeze_exclude_newer_packages(pyproject, lock) == set()
    assert pyproject.read_text(encoding="UTF-8") == before


def test_freeze_exclude_newer_packages_window_absorbs_same_day_patch(tmp_path):
    """A freeze holds a day-granular window, not a single version.

    The cutoff rounds up to the second UTC midnight after the held version's
    upload, so a patch released later the same day (or on the next calendar
    day) stays inside the window and is adopted on the next lock. This is the
    accepted trade-off documented on `_freeze_cutoff`: pinning the cutoff to
    the exact upload instant is the only way to reject a same-day patch, so
    locking the window width here makes any such tightening a conscious change.
    """
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = "0 day" }\n',
    )
    # Held version shipped mid-afternoon; a patch could land hours later, same day.
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-27T15:58:06Z"))
    freeze_exclude_newer_packages(pyproject, lock)
    assert (
        'exclude-newer-package = { mango = "2026-07-29T00:00:00Z" }'
        in pyproject.read_text(encoding="UTF-8")
    )
    # A same-day patch and a next-day release both fall before the cutoff, so uv
    # resolves up to them; a release two days on is excluded (window is bounded).
    cutoff = datetime(2026, 7, 29, tzinfo=timezone.utc)
    assert datetime(2026, 7, 27, 20, 15, tzinfo=timezone.utc) < cutoff
    assert datetime(2026, 7, 28, 23, 0, tzinfo=timezone.utc) < cutoff
    assert datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc) >= cutoff


def test_compute_bypass_forecasts_reports_freezes_only(tmp_path):
    """Fixed-timestamp freezes get an expiry; spans and dropped deps do not."""
    pyproject = _write_uv_config(
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
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { papaya = "2026-07-02T00:00:00Z" }\n',
    )
    lock = _write_lock(tmp_path, ("papaya", "1.0.0.dev0", ""))
    forecasts = compute_bypass_forecasts(pyproject, lock)
    assert forecasts == [BypassForecast("papaya", "1.0.0.dev0", BYPASS_NEEDS_RELEASE)]


def test_compute_bypass_forecasts_without_entries(tmp_path):
    """No `exclude-newer-package` table yields no forecasts."""
    pyproject = _write_uv_config(tmp_path, 'exclude-newer = "1 week"\n')
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


def test_parse_lock_specifiers_isolates_unconditional_declarations() -> None:
    """`by_main` holds only what a plain install of the project pulls in.

    A version marker leaves a dependency unconditional; an extra marker moves
    it to that extra's box wherever it sits in the expression; a dev group
    never lands there at all.
    """
    lock_data = {
        "package": [
            {
                "name": "my-project",
                "metadata": {
                    "requires-dist": [
                        {"name": "click", "specifier": ">=8.0"},
                        {
                            "name": "tomli",
                            "specifier": ">=2",
                            "marker": "python_full_version < '3.11'",
                        },
                        {
                            "name": "sphinx",
                            "specifier": ">=8",
                            "marker": "extra == 'sphinx'",
                        },
                        {
                            "name": "rich",
                            "specifier": ">=12.6",
                            "marker": (
                                "python_full_version >= '3.11' "
                                "and extra == 'screenshot'"
                            ),
                        },
                    ],
                    "requires-dev": {
                        "test": [{"name": "requests", "specifier": ">=2.34"}],
                    },
                },
            },
            {"name": "sphinx", "dependencies": [{"name": "requests"}]},
        ],
    }

    specs = parse_lock_specifiers(lock_data=lock_data)

    assert specs.by_main["my-project"] == {"click": ">=8.0", "tomli": ">=2"}
    assert specs.by_subgraph["sphinx"] == {"sphinx": ">=8"}
    assert specs.by_subgraph["screenshot"] == {"rich": ">=12.6"}
    assert specs.by_subgraph["test"] == {"requests": ">=2.34"}
    # Edge labels still need every declaration, dev groups included.
    assert specs.by_package["my-project"]["requests"] == ">=2.34"
    # A package the lockfile carries no metadata for is absent, not empty.
    assert "sphinx" not in specs.by_main
