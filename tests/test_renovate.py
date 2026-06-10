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

"""Tests for Renovate utilities module."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from repomatic.github.pr_body import sanitize_markdown_mentions
from repomatic.github.token import check_commit_statuses_permission
from repomatic.pypi import get_changelog_url
from repomatic.renovate import (
    RenovateCheckResult,
    check_dependabot_config_absent,
    check_dependabot_security_disabled,
    check_renovate_config_exists,
    collect_check_results,
    get_dependabot_config_path,
    run_migration_checks,
)
from repomatic.uv import (
    RELEASE_NOTES_MAX_LENGTH,
    _find_preceding_comments,
    _format_upload_date,
    _packages_outside_cooldown,
    _parse_github_owner_repo,
    _parse_iso_datetime,
    _parse_relative_duration,
    _resolve_exclude_newer_cutoff,
    _versions_in_range,
    add_exclude_newer_packages,
    build_comparison_urls,
    diff_lock_versions,
    fetch_release_notes,
    format_diff_table,
    format_release_notes,
    get_github_release_body,
    get_pypi_source_url,
    parse_lock_exclude_newer,
    parse_lock_upload_times,
    parse_lock_versions,
    prune_stale_exclude_newer_packages,
    sync_uv_lock,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


def test_no_config_file(tmp_path, monkeypatch):
    """Pass when no Dependabot config exists."""
    monkeypatch.chdir(tmp_path)
    passed, msg = check_dependabot_config_absent()
    assert passed is True
    assert "disabled" in msg


def test_yaml_exists(tmp_path, monkeypatch):
    """Fail when .github/dependabot.yaml exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yaml").touch()
    passed, msg = check_dependabot_config_absent()
    assert passed is False
    assert "dependabot.yaml" in msg


def test_yml_exists(tmp_path, monkeypatch):
    """Fail when .github/dependabot.yml exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yml").touch()
    passed, msg = check_dependabot_config_absent()
    assert passed is False
    assert "dependabot.yml" in msg


def test_disabled():
    """Pass when security updates disabled."""
    with patch("repomatic.renovate.run_gh_command") as mock_gh:
        mock_gh.return_value = "disabled\n"
        passed, msg = check_dependabot_security_disabled("owner/repo")
        assert passed is True
        assert "disabled" in msg


def test_enabled():
    """Fail when security updates enabled."""
    with patch("repomatic.renovate.run_gh_command") as mock_gh:
        mock_gh.return_value = "enabled\n"
        passed, msg = check_dependabot_security_disabled("owner/repo")
        assert passed is False
        assert "enabled" in msg.lower()


def test_api_failure():
    """Handle API failure gracefully."""
    with patch("repomatic.renovate.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("gh command failed")
        passed, _msg = check_dependabot_security_disabled("owner/repo")
        # Non-fatal, passes but with warning message.
        assert passed is True


def test_has_permission():
    """Pass when token has permission."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        passed, msg = check_commit_statuses_permission("owner/repo", "abc123")
        assert passed is True
        assert "access" in msg


def test_no_permission():
    """Fail when token lacks permission (HTTP 403)."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 403: Forbidden")
        passed, msg = check_commit_statuses_permission("owner/repo", "abc123")
        assert passed is False
        assert "permission" in msg.lower()


def _mock_all_perm_checks():
    """Return a context manager mocking check_all_pat_permissions with all passing."""
    from repomatic.github.token import PatPermissionResults

    perm_pass = (True, "Has access")
    result = PatPermissionResults(
        contents=perm_pass,
        issues=perm_pass,
        pull_requests=perm_pass,
        vulnerability_alerts=perm_pass,
        workflows=perm_pass,
        commit_statuses=perm_pass,
    )
    return patch(
        "repomatic.renovate.check_all_pat_permissions",
        return_value=result,
    )


def test_all_checks_pass(tmp_path, monkeypatch, capsys):
    """Return 0 when all checks pass."""
    monkeypatch.chdir(tmp_path)
    # Create renovate.json5 so that check passes.
    (tmp_path / "renovate.json5").touch()
    with (
        patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec,
        _mock_all_perm_checks(),
    ):
        mock_sec.return_value = (True, "Disabled")
        exit_code = run_migration_checks("owner/repo", "abc123")
        assert exit_code == 0


def test_renovate_config_missing(tmp_path, monkeypatch, capsys):
    """Return 1 when renovate.json5 is missing."""
    monkeypatch.chdir(tmp_path)
    with (
        patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec,
        _mock_all_perm_checks(),
    ):
        mock_sec.return_value = (True, "Disabled")
        exit_code = run_migration_checks("owner/repo", "abc123")
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "::error::" in captured.out


def test_dependabot_config_exists(tmp_path, monkeypatch, capsys):
    """Return 1 when Dependabot config exists."""
    monkeypatch.chdir(tmp_path)
    # Create renovate.json5 so that check passes.
    (tmp_path / "renovate.json5").touch()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yaml").touch()
    with (
        patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec,
        _mock_all_perm_checks(),
    ):
        mock_sec.return_value = (True, "Disabled")
        exit_code = run_migration_checks("owner/repo", "abc123")
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "::error::" in captured.out


def test_security_updates_enabled(tmp_path, monkeypatch, capsys):
    """Return 1 when Dependabot security updates are enabled."""
    monkeypatch.chdir(tmp_path)
    # Create renovate.json5 so that check passes.
    (tmp_path / "renovate.json5").touch()
    with (
        patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec,
        _mock_all_perm_checks(),
    ):
        mock_sec.return_value = (False, "Security updates enabled")
        exit_code = run_migration_checks("owner/repo", "abc123")
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "::error::" in captured.out


def test_config_exists(tmp_path, monkeypatch):
    """Pass when renovate.json5 exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "renovate.json5").touch()
    exists, msg = check_renovate_config_exists()
    assert exists is True
    assert "exists" in msg


def test_config_missing(tmp_path, monkeypatch):
    """Fail when renovate.json5 is missing."""
    monkeypatch.chdir(tmp_path)
    exists, msg = check_renovate_config_exists()
    assert exists is False
    assert "not found" in msg


def test_dependabot_yaml_config_path(tmp_path, monkeypatch):
    """Return path when .github/dependabot.yaml exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yaml").touch()
    path = get_dependabot_config_path()
    assert path is not None
    assert path.name == "dependabot.yaml"


def test_dependabot_yml_config_path(tmp_path, monkeypatch):
    """Return path when .github/dependabot.yml exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yml").touch()
    path = get_dependabot_config_path()
    assert path is not None
    assert path.name == "dependabot.yml"


def test_no_config(tmp_path, monkeypatch):
    """Return None when no Dependabot config exists."""
    monkeypatch.chdir(tmp_path)
    path = get_dependabot_config_path()
    assert path is None


def test_to_github_output():
    """Format results for GitHub Actions output."""
    result = RenovateCheckResult(
        renovate_config_exists=True,
        dependabot_config_path=".github/dependabot.yaml",
        dependabot_security_disabled=False,
        commit_statuses_permission=True,
        repo="owner/repo",
    )
    output = result.to_github_output()
    assert "renovate_config_exists=true" in output
    assert "dependabot_config_path=.github/dependabot.yaml" in output
    assert "dependabot_security_disabled=false" in output
    assert "commit_statuses_permission=true" in output
    assert "contents_permission=true" in output
    assert "issues_permission=true" in output
    assert "pull_requests_permission=true" in output
    assert "vulnerability_alerts_permission=true" in output
    assert "workflows_permission=true" in output
    assert "pr_body<<EOF" in output


def test_to_github_output_empty_path():
    """Empty dependabot_config_path is preserved in output."""
    result = RenovateCheckResult(
        renovate_config_exists=True,
        dependabot_config_path="",
        dependabot_security_disabled=True,
        commit_statuses_permission=True,
        repo="owner/repo",
    )
    output = result.to_github_output()
    assert "dependabot_config_path=" in output


def test_to_json():
    """Format results as JSON."""
    result = RenovateCheckResult(
        renovate_config_exists=True,
        dependabot_config_path=".github/dependabot.yaml",
        dependabot_security_disabled=False,
        commit_statuses_permission=True,
        repo="owner/repo",
    )
    json_str = result.to_json()
    data = json.loads(json_str)
    assert data["renovate_config_exists"] is True
    assert data["dependabot_config_path"] == ".github/dependabot.yaml"
    assert data["dependabot_security_disabled"] is False
    assert data["commit_statuses_permission"] is True
    assert data["contents_permission"] is True
    assert data["pull_requests_permission"] is True
    assert data["workflows_permission"] is True


def test_to_pr_body_needs_migration():
    """Generate PR body when migration is needed."""
    result = RenovateCheckResult(
        renovate_config_exists=False,
        dependabot_config_path=".github/dependabot.yaml",
        dependabot_security_disabled=False,
        commit_statuses_permission=True,
        pull_requests_permission=False,
        repo="owner/repo",
    )
    body = result.to_pr_body()
    assert "Migrate from Dependabot to Renovate" in body
    assert "Remove `.github/dependabot.yaml`" in body
    assert "ℹ️ Materialized at runtime" in body
    assert "🔧 Removed by this PR" in body
    assert "⚠️ Enabled" in body
    assert "https://github.com/owner/repo/settings/security_analysis" in body
    # Pull requests permission failed.
    assert "Pull requests permission" in body
    assert "⚠️ Cannot verify" in body


def test_to_pr_body_already_migrated():
    """Generate PR body when already migrated."""
    result = RenovateCheckResult(
        renovate_config_exists=True,
        dependabot_config_path="",
        dependabot_security_disabled=True,
        commit_statuses_permission=True,
        repo="owner/repo",
    )
    body = result.to_pr_body()
    assert "No changes needed" in body
    assert "✅ Already exists" in body
    assert "✅ Not present" in body
    assert "✅ Disabled" in body
    assert "✅ Token has access" in body


def test_collect_results_all_pass(tmp_path, monkeypatch):
    """Collect results when all checks pass."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "renovate.json5").touch()
    with (
        patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec,
        _mock_all_perm_checks(),
    ):
        mock_sec.return_value = (True, "Disabled")
        result = collect_check_results("owner/repo", "abc123")
        assert result.renovate_config_exists is True
        assert result.dependabot_config_path == ""
        assert result.dependabot_security_disabled is True
        assert result.commit_statuses_permission is True
        assert result.contents_permission is True
        assert result.pull_requests_permission is True
        assert result.workflows_permission is True


def test_with_dependabot_config(tmp_path, monkeypatch):
    """Collect results when Dependabot config exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "renovate.json5").touch()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yaml").touch()
    with (
        patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec,
        _mock_all_perm_checks(),
    ):
        mock_sec.return_value = (True, "Disabled")
        result = collect_check_results("owner/repo", "abc123")
        assert result.dependabot_config_path == ".github/dependabot.yaml"


def test_missing_renovate_config(tmp_path, monkeypatch):
    """Collect results when renovate.json5 is missing."""
    monkeypatch.chdir(tmp_path)
    with (
        patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec,
        _mock_all_perm_checks(),
    ):
        mock_sec.return_value = (True, "Disabled")
        result = collect_check_results("owner/repo", "abc123")
        assert result.renovate_config_exists is False


def test_sync_uv_lock_no_changes(tmp_path):
    """Return empty changes when no package versions changed."""
    lock_path = tmp_path / "uv.lock"
    with (
        patch("repomatic.uv.subprocess.run") as mock_run,
        patch("repomatic.uv.parse_lock_versions", return_value={}),
    ):
        result = sync_uv_lock(lock_path)
        assert result.changes == []
        # uv lock was called in the project directory.
        mock_run.assert_called_once_with(
            ["uv", "--no-progress", "lock", "--upgrade"],
            check=True,
            cwd=tmp_path,
        )


def test_sync_uv_lock_returns_changes(tmp_path):
    """Return structured changes when package versions changed."""
    lock_path = tmp_path / "uv.lock"
    before = {"anyio": "4.12.0", "boltons": "25.0.0"}
    after = {"anyio": "4.12.1", "boltons": "25.0.0"}
    with (
        patch("repomatic.uv.subprocess.run"),
        patch("repomatic.uv.parse_lock_versions", side_effect=[before, after]),
    ):
        result = sync_uv_lock(lock_path)
        assert len(result.changes) == 1
        assert result.changes[0] == ("anyio", "4.12.0", "4.12.1")


def test_parse_lock_versions(tmp_path):
    """Parse package names and versions from a uv.lock file."""
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        'version = 1\nrequires-python = ">=3.10"\n\n'
        "[[package]]\n"
        'name = "anyio"\n'
        'version = "4.12.0"\n\n'
        "[[package]]\n"
        'name = "boltons"\n'
        'version = "25.0.0"\n',
        encoding="UTF-8",
    )
    versions = parse_lock_versions(lock_path)
    assert versions == {"anyio": "4.12.0", "boltons": "25.0.0"}


def test_parse_lock_versions_missing_file(tmp_path):
    """Return empty dict when the lock file does not exist."""
    lock_path = tmp_path / "uv.lock"
    assert parse_lock_versions(lock_path) == {}


def test_diff_lock_versions():
    """Detect added, removed, and updated packages."""
    before = {"anyio": "4.12.0", "boltons": "25.0.0", "old-pkg": "1.0.0"}
    after = {"anyio": "4.12.1", "boltons": "25.0.0", "new-pkg": "2.0.0"}
    changes = diff_lock_versions(before, after)
    assert ("anyio", "4.12.0", "4.12.1") in changes
    assert ("new-pkg", "", "2.0.0") in changes
    assert ("old-pkg", "1.0.0", "") in changes
    # Unchanged package is not in the diff.
    assert all(name != "boltons" for name, _, _ in changes)


def test_diff_lock_versions_no_changes():
    """Return empty list when versions are identical."""
    versions = {"anyio": "4.12.0"}
    assert diff_lock_versions(versions, versions) == []


def test_format_diff_table_empty():
    """Return empty string for no changes."""
    assert format_diff_table([]) == ""


def test_format_diff_table():
    """Format a markdown table with version changes and no upload times."""
    changes = [
        ("anyio", "4.12.0", "4.12.1"),
        ("new-pkg", "", "2.0.0"),
        ("old-pkg", "1.0.0", ""),
    ]
    table = format_diff_table(changes)
    assert "### Updated packages" in table
    assert "| Package | Change |" in table
    assert "| [anyio](https://pypi.org/project/anyio/) | `4.12.0` → `4.12.1` |" in table
    assert "| [new-pkg](https://pypi.org/project/new-pkg/) | (new) `2.0.0` |" in table
    assert (
        "| [old-pkg](https://pypi.org/project/old-pkg/) | `1.0.0` (removed) |" in table
    )
    assert "Released" not in table
    assert "exclude-newer" not in table


def test_format_diff_table_with_upload_times():
    """Format a markdown table with upload times and exclude-newer cutoff."""
    changes = [
        ("coverage", "7.13.4", "7.13.5"),
        ("anyio", "4.12.0", "4.12.1"),
    ]
    upload_times = {
        "coverage": "2026-03-13T18:30:00Z",
        "anyio": "2026-01-06T11:45:21.246Z",
    }
    table = format_diff_table(
        changes,
        upload_times=upload_times,
        exclude_newer="2026-03-18T16:39:02.780682017Z",
    )
    assert (
        "Resolved with [`exclude-newer`]"
        "(https://docs.astral.sh/uv/reference/settings/#exclude-newer)"
        " cutoff: `2026-03-18`." in table
    )
    assert "| Package | Change | Released |" in table
    assert (
        "| [coverage](https://pypi.org/project/coverage/)"
        " | `7.13.4` → `7.13.5` | 2026-03-13 |"
    ) in table
    assert (
        "| [anyio](https://pypi.org/project/anyio/)"
        " | `4.12.0` → `4.12.1` | 2026-01-06 |"
    ) in table


def test_format_diff_table_upload_times_without_exclude_newer():
    """Upload times column appears even without exclude-newer."""
    changes = [("pkg", "1.0", "2.0")]
    upload_times = {"pkg": "2026-03-01T00:00:00Z"}
    table = format_diff_table(changes, upload_times=upload_times)
    assert "| Released |" in table
    assert "2026-03-01" in table
    assert "exclude-newer" not in table


def test_format_upload_date():
    """Format ISO 8601 datetime to date-only string."""
    assert _format_upload_date("2026-03-13T18:30:00Z") == "2026-03-13"
    assert _format_upload_date("2026-03-18T16:39:02.780682017Z") == "2026-03-18"
    assert _format_upload_date("2026-01-06T11:45:21.246Z") == "2026-01-06"
    # Graceful fallback for unparsable input.
    assert _format_upload_date("not-a-date") == "not-a-date"
    assert _format_upload_date("") == ""


def test_parse_lock_exclude_newer(tmp_path):
    """Extract exclude-newer from a lock file."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        "[options]\n"
        'exclude-newer = "2026-03-18T16:39:02.780682017Z"\n\n'
        '[[package]]\nname = "foo"\nversion = "1.0"\n'
    )
    assert parse_lock_exclude_newer(lock) == "2026-03-18T16:39:02.780682017Z"


def test_parse_lock_exclude_newer_missing(tmp_path):
    """Return empty string when exclude-newer is absent."""
    lock = tmp_path / "uv.lock"
    lock.write_text('version = 1\n\n[[package]]\nname = "foo"\nversion = "1.0"\n')
    assert parse_lock_exclude_newer(lock) == ""


def test_parse_lock_exclude_newer_span_fallback(tmp_path):
    """Fall back to `exclude-newer-span` when the sentinel timestamp is set."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        "[options]\n"
        'exclude-newer = "0001-01-01T00:00:00Z"\n'
        'exclude-newer-span = "P1W"\n\n'
        '[[package]]\nname = "foo"\nversion = "1.0"\n'
    )
    result = parse_lock_exclude_newer(lock)
    cutoff = datetime.fromisoformat(result.replace("Z", "+00:00"))
    expected = datetime.now(timezone.utc) - timedelta(weeks=1)
    assert abs((cutoff - expected).total_seconds()) < 60


def test_parse_lock_exclude_newer_sentinel_without_span(tmp_path):
    """Return empty string when only the sentinel is present without a span."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        "[options]\n"
        'exclude-newer = "0001-01-01T00:00:00Z"\n\n'
        '[[package]]\nname = "foo"\nversion = "1.0"\n'
    )
    assert parse_lock_exclude_newer(lock) == ""


def test_add_exclude_newer_packages_appends(tmp_path):
    """New packages are appended to an existing inline table."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { "click-extra" = "0 day" }\n'
    )
    assert add_exclude_newer_packages(pyproject, {"pygments"}) is True
    content = pyproject.read_text()
    parsed = tomllib.loads(content)
    pkg = parsed["tool"]["uv"]["exclude-newer-package"]
    assert pkg["pygments"] == "0 day"
    assert pkg["click-extra"] == "0 day"
    # Verify pyproject-fmt-compatible formatting: spaces inside braces,
    # single space after commas, sorted keys.
    assert (
        'exclude-newer-package = { click-extra = "0 day", pygments = "0 day" }'
        in content
    )


def test_add_exclude_newer_packages_skips_existing(tmp_path):
    """Packages already in the table are not duplicated."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { "pygments" = "0 day" }\n'
    )
    assert add_exclude_newer_packages(pyproject, {"pygments"}) is False


def test_add_exclude_newer_packages_creates_line(tmp_path):
    """A new exclude-newer-package line is inserted when none exists."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.uv]\nexclude-newer = "1 week"\n')
    assert add_exclude_newer_packages(pyproject, {"requests"}) is True
    content = pyproject.read_text()
    # Verify pyproject-fmt-compatible formatting.
    assert 'exclude-newer-package = { requests = "0 day" }' in content
    parsed = tomllib.loads(content)
    assert parsed["tool"]["uv"]["exclude-newer-package"]["requests"] == "0 day"


def test_add_exclude_newer_packages_inserts_after_exclude_newer(tmp_path):
    """A new entry is placed right after `exclude-newer`, matching pyproject-fmt."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        "# Cutoff comment.\n"
        'exclude-newer = "1 week"\n'
        "# Root-level build comment.\n"
        'build-backend.module-root = ""\n'
    )
    assert add_exclude_newer_packages(pyproject, {"urllib3"}) is True
    content = pyproject.read_text()
    expected = (
        "[tool.uv]\n"
        "# Cutoff comment.\n"
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { urllib3 = "0 day" }\n'
        "# Root-level build comment.\n"
        'build-backend.module-root = ""\n'
    )
    assert content == expected


def test_add_exclude_newer_packages_multiple(tmp_path):
    """Multiple packages are added in sorted order."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { "click-extra" = "0 day" }\n'
    )
    assert add_exclude_newer_packages(pyproject, {"requests", "pygments"}) is True
    content = pyproject.read_text()
    parsed = tomllib.loads(content)
    pkg = parsed["tool"]["uv"]["exclude-newer-package"]
    assert pkg["pygments"] == "0 day"
    assert pkg["requests"] == "0 day"
    assert pkg["click-extra"] == "0 day"
    # All three entries sorted, pyproject-fmt-compatible formatting.
    assert (
        'exclude-newer-package = { click-extra = "0 day",'
        ' pygments = "0 day", requests = "0 day" }' in content
    )


def test_add_exclude_newer_packages_no_uv_section(tmp_path):
    """Returns False when no exclude-newer configuration exists."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'foo'\n")
    assert add_exclude_newer_packages(pyproject, {"requests"}) is False


def test_packages_outside_cooldown_filters_reachable(tmp_path):
    """Packages whose upload time is before the cutoff are not exempted."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.uv]\nexclude-newer = "2026-04-01T00:00:00Z"\n')
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "pygments"\nversion = "2.20.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/pygments.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2026-03-29T00:00:00Z"\n\n'
        '[[package]]\nname = "requests"\nversion = "2.33.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/requests.tar.gz"\n'
        'hash = "sha256:def"\n'
        'upload-time = "2026-03-25T00:00:00Z"\n'
    )
    result = _packages_outside_cooldown(
        pyproject,
        lock,
        {"pygments", "requests"},
    )
    assert result == set()


def test_packages_outside_cooldown_keeps_unreachable(tmp_path):
    """Packages uploaded after the cutoff need an exemption."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.uv]\nexclude-newer = "2026-03-25T00:00:00Z"\n')
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "pygments"\nversion = "2.20.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/pygments.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2026-03-29T00:00:00Z"\n\n'
        '[[package]]\nname = "requests"\nversion = "2.33.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/requests.tar.gz"\n'
        'hash = "sha256:def"\n'
        'upload-time = "2026-03-20T00:00:00Z"\n'
    )
    result = _packages_outside_cooldown(
        pyproject,
        lock,
        {"pygments", "requests"},
    )
    # Only pygments is after the cutoff.
    assert result == {"pygments"}


def test_packages_outside_cooldown_no_upload_time(tmp_path):
    """Packages without upload times are always exempted."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.uv]\nexclude-newer = "2026-04-01T00:00:00Z"\n')
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "repomatic"\nversion = "6.8.0"\n'
        'source = { git = "https://github.com/kdeldycke/repomatic" }\n'
    )
    result = _packages_outside_cooldown(pyproject, lock, {"repomatic"})
    assert result == {"repomatic"}


def test_packages_outside_cooldown_no_exclude_newer(tmp_path):
    """All packages are exempted when no exclude-newer is configured."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.uv]\n")
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "requests"\nversion = "2.33.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/requests.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2026-03-20T00:00:00Z"\n'
    )
    result = _packages_outside_cooldown(pyproject, lock, {"requests"})
    assert result == {"requests"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0 day", timedelta(days=0)),
        ("1 day", timedelta(days=1)),
        ("3 days", timedelta(days=3)),
        ("1 week", timedelta(weeks=1)),
        ("2 weeks", timedelta(weeks=2)),
        ("1 hour", timedelta(hours=1)),
        ("24 hours", timedelta(hours=24)),
        ("1 minute", timedelta(minutes=1)),
        ("30 minutes", timedelta(minutes=30)),
        ("1 second", timedelta(seconds=1)),
        ("60 seconds", timedelta(seconds=60)),
    ],
)
def test_parse_relative_duration(value, expected):
    """Parse uv friendly relative duration strings."""
    assert _parse_relative_duration(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "2026-03-18T16:39:02Z",
        "not-a-duration",
        "1 month",
        "1 year",
        "",
    ],
)
def test_parse_relative_duration_returns_none(value):
    """Return None for non-duration strings and disallowed units."""
    assert _parse_relative_duration(value) is None


def test_parse_iso_datetime():
    """Parse ISO 8601 with nanosecond truncation."""
    result = _parse_iso_datetime("2026-03-18T16:39:02.780682017Z")
    assert result is not None
    assert result.year == 2026
    assert result.month == 3
    assert result.day == 18


def test_parse_iso_datetime_invalid():
    """Return None for invalid strings."""
    assert _parse_iso_datetime("not-a-date") is None
    assert _parse_iso_datetime("") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1 week", timedelta(weeks=1)),
        ("24 hours", timedelta(hours=24)),
        ("30 minutes", timedelta(minutes=30)),
        ("PT24H", timedelta(hours=24)),
        ("P7D", timedelta(days=7)),
        ("P1W", timedelta(weeks=1)),
        ("P1DT2H", timedelta(days=1, hours=2)),
    ],
)
def test_resolve_exclude_newer_cutoff_durations(value, expected):
    """A duration in any accepted form resolves to `now - duration`."""
    before = datetime.now(timezone.utc) - expected
    cutoff = _resolve_exclude_newer_cutoff(value)
    after = datetime.now(timezone.utc) - expected
    assert cutoff is not None
    # `now - duration` is sandwiched between the two timestamps captured
    # around the call.
    assert before <= cutoff <= after


def test_resolve_exclude_newer_cutoff_timestamp():
    """An RFC 3339 timestamp is returned verbatim as the cutoff."""
    cutoff = _resolve_exclude_newer_cutoff("2026-03-18T16:39:02Z")
    assert cutoff is not None
    assert cutoff.year == 2026
    assert cutoff.month == 3
    assert cutoff.day == 18


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-value",
        "1 month",
        "P1M",
    ],
)
def test_resolve_exclude_newer_cutoff_invalid(value):
    """Empty, unrecognized, or calendar-unit values return `None`."""
    assert _resolve_exclude_newer_cutoff(value) is None


def test_prune_stale_removes_old_entry(tmp_path):
    """Remove entries whose upload time is before the cutoff."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "2026-03-25T00:00:00Z"\n'
        'exclude-newer-package = { "pygments" = "0 day",'
        ' "repomatic" = "0 day" }\n'
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "pygments"\nversion = "2.19.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/pygments.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2026-03-01T00:00:00Z"\n\n'
        '[[package]]\nname = "repomatic"\nversion = "6.8.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/repomatic.tar.gz"\n'
        'hash = "sha256:def"\n'
        'upload-time = "2026-03-26T00:00:00Z"\n'
    )
    assert prune_stale_exclude_newer_packages(pyproject, lock) is True
    content = pyproject.read_text()
    parsed = tomllib.loads(content)
    pkg = parsed["tool"]["uv"]["exclude-newer-package"]
    assert "pygments" not in pkg
    assert pkg["repomatic"] == "0 day"
    # Verify pyproject-fmt-compatible formatting after pruning.
    assert 'exclude-newer-package = { repomatic = "0 day" }' in content


def test_prune_stale_removes_all_entries(tmp_path):
    """Remove the entire key and its preceding comment when all entries are stale."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "2026-03-25T00:00:00Z"\n'
        "# Packages that bypass the cooldown window.\n"
        'exclude-newer-package = { "pygments" = "0 day" }\n'
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "pygments"\nversion = "2.19.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/pygments.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2026-03-01T00:00:00Z"\n'
    )
    assert prune_stale_exclude_newer_packages(pyproject, lock) is True
    content = pyproject.read_text()
    assert "exclude-newer-package" not in content
    assert "bypass the cooldown" not in content


def test_prune_stale_keeps_no_upload_time(tmp_path):
    """Keep entries for packages without upload times (git sources)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "2026-03-25T00:00:00Z"\n'
        'exclude-newer-package = { "repomatic" = "0 day" }\n'
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "repomatic"\nversion = "6.8.0"\n'
        'source = { git = "https://github.com/kdeldycke/repomatic" }\n'
    )
    assert prune_stale_exclude_newer_packages(pyproject, lock) is False


def test_prune_stale_relative_duration(tmp_path):
    """Handle relative exclude-newer values like '1 week'."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { "old-pkg" = "0 day" }\n'
    )
    lock = tmp_path / "uv.lock"
    # Upload time far in the past, well before any 1-week window.
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "old-pkg"\nversion = "1.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/old.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2020-01-01T00:00:00Z"\n'
    )
    assert prune_stale_exclude_newer_packages(pyproject, lock) is True
    assert "exclude-newer-package" not in pyproject.read_text()


def test_prune_stale_nothing_stale(tmp_path):
    """Return False when all entries are still needed."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "2026-03-25T00:00:00Z"\n'
        'exclude-newer-package = { "pygments" = "0 day" }\n'
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "pygments"\nversion = "2.20.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/pygments.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2026-03-28T00:00:00Z"\n'
    )
    assert prune_stale_exclude_newer_packages(pyproject, lock) is False


def test_prune_stale_no_entries(tmp_path):
    """Return False when there are no exclude-newer-package entries."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.uv]\nexclude-newer = "1 week"\n')
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n")
    assert prune_stale_exclude_newer_packages(pyproject, lock) is False


def test_find_preceding_comments():
    """Find comment lines immediately above a TOML key."""
    text = (
        'exclude-newer = "1 week"\n'
        "# Packages that bypass the cooldown.\n"
        'exclude-newer-package = { "requests" = "0 day" }\n'
    )
    assert _find_preceding_comments(text, "exclude-newer-package") == (
        "# Packages that bypass the cooldown.\n"
    )


def test_find_preceding_comments_none():
    """Return empty string when no comment precedes the key."""
    text = (
        'exclude-newer = "1 week"\nexclude-newer-package = { "requests" = "0 day" }\n'
    )
    assert _find_preceding_comments(text, "exclude-newer-package") == ""


def test_find_preceding_comments_multiple():
    """Find multiple consecutive comment lines above a key."""
    text = (
        "# First line.\n"
        "# Second line.\n"
        'exclude-newer-package = { "requests" = "0 day" }\n'
    )
    result = _find_preceding_comments(text, "exclude-newer-package")
    assert result == "# First line.\n# Second line.\n"


# ---------------------------------------------------------------------------
# _versions_in_range
# ---------------------------------------------------------------------------


def test_versions_in_range():
    """Return versions in the half-open range (old, new]."""
    with patch("repomatic.uv.get_pypi_release_dates") as mock_dates:
        mock_dates.return_value = {
            "11.0.2": None,
            "11.0.3": None,
            "11.0.4": None,
            "11.0.5": None,
            "11.0.6": None,
        }
        result = _versions_in_range("extra-platforms", "11.0.3", "11.0.5")
    assert result == ["11.0.4", "11.0.5"]


def test_versions_in_range_no_intermediate():
    """Fall back to [new] when no intermediate versions exist."""
    with patch("repomatic.uv.get_pypi_release_dates") as mock_dates:
        mock_dates.return_value = {"1.0": None, "2.0": None}
        result = _versions_in_range("pkg", "1.0", "2.0")
    assert result == ["2.0"]


def test_versions_in_range_pypi_failure():
    """Fall back to [new] when PyPI is unreachable."""
    with patch("repomatic.uv.get_pypi_release_dates", return_value={}):
        result = _versions_in_range("pkg", "1.0", "2.0")
    assert result == ["2.0"]


# ---------------------------------------------------------------------------
# build_comparison_urls
# ---------------------------------------------------------------------------


def test_build_comparison_urls():
    """Build comparison URLs from changes and release notes."""
    changes = [
        ("pkg-a", "1.0", "2.0"),
        ("pkg-b", "3.0", "4.0"),
        ("new-pkg", "", "1.0"),
    ]
    notes = {
        "pkg-a": ("https://github.com/owner/pkg-a", [("v2.0", "Notes.")]),
        "pkg-b": ("https://github.com/owner/pkg-b", [("4.0", "Notes.")]),
    }
    urls = build_comparison_urls(changes, notes)
    assert urls["pkg-a"] == "https://github.com/owner/pkg-a/compare/v1.0...v2.0"
    assert urls["pkg-b"] == "https://github.com/owner/pkg-b/compare/3.0...4.0"
    # New packages (no old version) are excluded.
    assert "new-pkg" not in urls


def test_build_comparison_urls_changelog_fallback():
    """Packages with changelog fallback (no tag) default to v-prefix."""
    changes = [("pkg", "1.0", "2.0")]
    notes = {
        "pkg": (
            "https://github.com/owner/pkg",
            [("", "[Changelog](https://example.com)")],
        ),
    }
    urls = build_comparison_urls(changes, notes)
    assert urls["pkg"] == "https://github.com/owner/pkg/compare/v1.0...v2.0"


def test_format_diff_table_with_comparison_urls():
    """Wrap version changes with comparison links when URLs are provided."""
    changes = [("pkg", "1.0", "2.0")]
    urls = {"pkg": "https://github.com/owner/pkg/compare/v1.0...v2.0"}
    table = format_diff_table(changes, comparison_urls=urls)
    assert (
        "[`1.0` \u2192 `2.0`](https://github.com/owner/pkg/compare/v1.0...v2.0)"
        in table
    )


def test_format_release_notes_multiple_versions():
    """Render multiple version entries per package."""
    notes = {
        "pkg": (
            "https://github.com/owner/pkg",
            [
                ("v1.1", "Patch notes."),
                ("v1.2", "Feature notes."),
            ],
        ),
    }
    result = format_release_notes(notes)
    assert "<summary><code>pkg</code></summary>" in result
    assert "[`v1.1`](https://github.com/owner/pkg/releases/tag/v1.1)" in result
    assert "[`v1.2`](https://github.com/owner/pkg/releases/tag/v1.2)" in result
    assert "Patch notes." in result
    assert "Feature notes." in result


def test_parse_lock_upload_times(tmp_path):
    """Extract upload times from a lock file's sdist entries."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "foo"\nversion = "1.0"\n'
        "[package.sdist]\n"
        'url = "https://example.com/foo-1.0.tar.gz"\n'
        'hash = "sha256:abc"\n'
        'upload-time = "2026-03-13T18:30:00Z"\n\n'
        '[[package]]\nname = "bar"\nversion = "2.0"\n'
    )
    times = parse_lock_upload_times(lock)
    assert times == {"foo": "2026-03-13T18:30:00Z"}
    assert "bar" not in times


# --- Release notes tests ---


def test_parse_github_owner_repo():
    """Extract owner/repo from various GitHub URL forms."""
    assert _parse_github_owner_repo("https://github.com/nedbat/coveragepy") == (
        "nedbat",
        "coveragepy",
    )
    assert _parse_github_owner_repo("https://github.com/foo/bar/") == ("foo", "bar")
    assert _parse_github_owner_repo("https://github.com/foo/bar.git") == ("foo", "bar")
    assert _parse_github_owner_repo("short") is None


def _make_urlopen_mock(responses):
    """Build a side_effect function for mocking `urlopen`.

    :param responses: A dict mapping URL substrings to (status, body) tuples.
        If body is `None`, a `URLError` is raised for that URL.
    """
    from io import BytesIO
    from urllib.error import URLError

    class FakeResponse:
        def __init__(self, data):
            self._data = BytesIO(data)

        def read(self):
            return self._data.read()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def side_effect(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for pattern, (body,) in responses.items():
            if pattern in url:
                if body is None:
                    raise URLError(f"Mocked failure for {url}")
                return FakeResponse(body)
        raise URLError(f"No mock for {url}")

    return side_effect


def test_get_pypi_source_url_found():
    """Discover GitHub URL from PyPI project_urls."""
    pypi_data = json.dumps({
        "info": {
            "project_urls": {
                "Homepage": "https://coverage.readthedocs.io",
                "Source": "https://github.com/nedbat/coveragepy",
            },
        },
    }).encode()
    mock = _make_urlopen_mock({"pypi.org": (pypi_data,)})
    with (
        patch("repomatic.pypi.urlopen", side_effect=mock),
        patch("repomatic.pypi.get_cached_response", return_value=None),
    ):
        assert get_pypi_source_url("coverage") == "https://github.com/nedbat/coveragepy"


def test_get_pypi_source_url_no_github():
    """Return None when no GitHub URL in project_urls."""
    pypi_data = json.dumps({
        "info": {
            "project_urls": {
                "Homepage": "https://example.com",
            },
        },
    }).encode()
    mock = _make_urlopen_mock({"pypi.org": (pypi_data,)})
    with (
        patch("repomatic.pypi.urlopen", side_effect=mock),
        patch("repomatic.pypi.get_cached_response", return_value=None),
    ):
        assert get_pypi_source_url("somepkg") is None


def test_get_pypi_source_url_api_failure():
    """Return None on PyPI API failure."""
    mock = _make_urlopen_mock({"pypi.org": (None,)})
    with (
        patch("repomatic.pypi.urlopen", side_effect=mock),
        patch("repomatic.pypi.get_cached_response", return_value=None),
    ):
        assert get_pypi_source_url("coverage") is None


def test_get_changelog_url_found():
    """Discover changelog URL from PyPI project_urls."""
    pypi_data = json.dumps({
        "info": {
            "project_urls": {
                "Changelog": "https://github.com/dpranke/pyjson5/blob/master/README.md",
                "Repository": "https://github.com/dpranke/pyjson5",
            },
        },
    }).encode()
    mock = _make_urlopen_mock({"pypi.org": (pypi_data,)})
    with patch("repomatic.pypi.urlopen", side_effect=mock):
        assert get_changelog_url("json5") == (
            "https://github.com/dpranke/pyjson5/blob/master/README.md"
        )


def test_get_changelog_url_not_found():
    """Return None when no changelog key in project_urls."""
    pypi_data = json.dumps({
        "info": {
            "project_urls": {
                "Homepage": "https://github.com/stefankoegl/python-json-pointer",
            },
        },
    }).encode()
    mock = _make_urlopen_mock({"pypi.org": (pypi_data,)})
    with patch("repomatic.pypi.urlopen", side_effect=mock):
        assert get_changelog_url("jsonpointer") is None


def test_get_github_release_body_found():
    """Fetch release body from GitHub for v-prefixed tag."""
    release_data = json.dumps({"body": "### Bug fixes\n- Fixed a bug."}).encode()
    mock = _make_urlopen_mock({"releases/tags/v7.13.5": (release_data,)})
    with patch("repomatic.uv.urlopen", side_effect=mock):
        tag, body = get_github_release_body(
            "https://github.com/nedbat/coveragepy",
            "7.13.5",
        )
    assert tag == "v7.13.5"
    assert "Fixed a bug" in body


def test_get_github_release_body_bare_tag_fallback():
    """Fall back to bare version tag when v-prefixed tag is not found."""
    release_data = json.dumps({"body": "Release notes."}).encode()

    def side_effect(request, timeout=None):
        from urllib.error import URLError

        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "tags/v2.0" in url:
            raise URLError("Not found")
        if "tags/2.0" in url:

            class FakeResp:
                def read(self):
                    return release_data

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return FakeResp()
        raise URLError(f"No mock for {url}")

    with patch("repomatic.uv.urlopen", side_effect=side_effect):
        tag, body = get_github_release_body(
            "https://github.com/owner/repo",
            "2.0",
        )
    assert tag == "2.0"
    assert body == "Release notes."


def test_get_github_release_body_not_found():
    """Return empty strings when no release exists for any tag variant."""
    mock = _make_urlopen_mock({
        "tags/v": (None,),
        "tags/1": (None,),
    })
    with patch("repomatic.uv.urlopen", side_effect=mock):
        tag, body = get_github_release_body(
            "https://github.com/owner/repo",
            "1.0",
        )
    assert tag == ""
    assert body == ""


def test_fetch_release_notes_skips_removals():
    """Removed packages (empty new_version) are not fetched."""
    changes = [("old-pkg", "1.0.0", "")]
    with patch("repomatic.uv.get_pypi_source_url") as mock_pypi:
        notes = fetch_release_notes(changes)
    mock_pypi.assert_not_called()
    assert notes == {}


def test_fetch_release_notes_aggregation():
    """Aggregate release notes from multiple packages."""
    changes = [
        ("pkg-a", "1.0", "2.0"),
        ("pkg-b", "3.0", "4.0"),
    ]
    with (
        patch("repomatic.uv.get_pypi_source_url") as mock_pypi,
        patch("repomatic.uv.get_github_release_body") as mock_gh,
        patch("repomatic.uv.get_pypi_changelog_url", return_value=None),
        patch("repomatic.uv._versions_in_range") as mock_range,
    ):
        mock_pypi.side_effect = [
            "https://github.com/owner/pkg-a",
            "https://github.com/owner/pkg-b",
        ]
        mock_range.side_effect = [["2.0"], ["4.0"]]
        mock_gh.side_effect = [
            ("v2.0", "Notes for A."),
            ("v4.0", ""),
        ]
        notes = fetch_release_notes(changes)
    assert "pkg-a" in notes
    assert notes["pkg-a"] == (
        "https://github.com/owner/pkg-a",
        [("v2.0", "Notes for A.")],
    )
    # pkg-b has empty body and no changelog URL, so it should be excluded.
    assert "pkg-b" not in notes


def test_fetch_release_notes_changelog_fallback():
    """Fall back to PyPI changelog URL when no GitHub Release exists."""
    changes = [("json5", "0.13.0", "0.14.0")]
    changelog = "https://github.com/dpranke/pyjson5/blob/master/README.md"
    with (
        patch("repomatic.uv.get_pypi_source_url") as mock_pypi,
        patch("repomatic.uv.get_github_release_body") as mock_gh,
        patch("repomatic.uv.get_pypi_changelog_url") as mock_cl,
        patch("repomatic.uv._versions_in_range", return_value=["0.14.0"]),
    ):
        mock_pypi.return_value = "https://github.com/dpranke/pyjson5"
        mock_gh.return_value = ("", "")
        mock_cl.return_value = changelog
        notes = fetch_release_notes(changes)
    assert "json5" in notes
    repo_url, versions = notes["json5"]
    assert repo_url == "https://github.com/dpranke/pyjson5"
    assert versions[0][0] == ""
    assert changelog in versions[0][1]


def test_format_release_notes():
    """Render release notes as collapsible details blocks."""
    notes = {
        "coverage": (
            "https://github.com/nedbat/coveragepy",
            [("v7.13.5", "### Bug fixes\n- Fixed a bug.")],
        ),
    }
    result = format_release_notes(notes)
    assert "### Release notes" in result
    assert "<details>" in result
    assert "<summary><code>coverage</code></summary>" in result
    assert (
        "[`v7.13.5`](https://github.com/nedbat/coveragepy/releases/tag/v7.13.5)"
        in result
    )
    assert "Fixed a bug" in result
    assert "</details>" in result


def test_format_release_notes_empty():
    """Return empty string when no notes are available."""
    assert format_release_notes({}) == ""


def test_format_release_notes_changelog_fallback():
    """Render a changelog link when no GitHub Release tag exists."""
    notes = {
        "json5": (
            "https://github.com/dpranke/pyjson5",
            [
                (
                    "",
                    "[Changelog](https://github.com/dpranke/pyjson5/blob/master/README.md)",
                )
            ],
        ),
    }
    result = format_release_notes(notes)
    assert "<details>" in result
    assert "<summary><code>json5</code></summary>" in result
    # GitHub URLs in the body are rewritten to prevent backlink cross-references.
    assert (
        "[Changelog]"
        "(https://redirect.github.com/dpranke/pyjson5/blob/master/README.md)" in result
    )
    # No tag heading should appear.
    assert "#### [" not in result
    assert "</details>" in result


def test_format_release_notes_truncation():
    """Truncate long release bodies with a link to the full release."""
    long_body = "Line\n" * (RELEASE_NOTES_MAX_LENGTH // 5 + 100)
    notes = {
        "pkg": ("https://github.com/owner/pkg", [("v1.0", long_body)]),
    }
    result = format_release_notes(notes)
    assert "Full release notes" in result
    assert "https://github.com/owner/pkg/releases/tag/v1.0" in result


# ---------------------------------------------------------------------------
# sanitize_markdown_mentions
# ---------------------------------------------------------------------------

ZWS = "\u200b"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            "Thanks @alice for the fix!",
            f"Thanks @{ZWS}alice for the fix!",
            id="simple-mention",
        ),
        pytest.param(
            "@bob opened this PR",
            f"@{ZWS}bob opened this PR",
            id="mention-at-line-start",
        ),
        pytest.param(
            "cc @alice @bob @charlie",
            f"cc @{ZWS}alice @{ZWS}bob @{ZWS}charlie",
            id="multiple-mentions",
        ),
        pytest.param(
            "Thanks @org/team-name for reviewing",
            f"Thanks @{ZWS}org/team-name for reviewing",
            id="team-mention",
        ),
        pytest.param(
            "Fixes #42 and #100",
            f"Fixes #{ZWS}42 and #{ZWS}100",
            id="issue-refs",
        ),
        pytest.param(
            "Fix #42",
            f"Fix #{ZWS}42",
            id="fix-keyword-issue-ref",
        ),
        pytest.param(
            "See https://github.com/owner/repo/issues/1",
            "See https://redirect.github.com/owner/repo/issues/1",
            id="github-url-rewrite",
        ),
        pytest.param(
            "http://github.com/owner/repo/pull/5",
            "http://redirect.github.com/owner/repo/pull/5",
            id="github-url-http",
        ),
        pytest.param(
            "Thanks @alice! Fixes #42. See https://github.com/org/repo",
            f"Thanks @{ZWS}alice! Fixes #{ZWS}42."
            " See https://redirect.github.com/org/repo",
            id="combined-all-three",
        ),
        pytest.param(
            "Use this:\n```python\n@pytest.mark.parametrize\n```\n",
            "Use this:\n```python\n@pytest.mark.parametrize\n```\n",
            id="fenced-code-block-preserved",
        ),
        pytest.param(
            "Example:\n~~~\n@user #123\n~~~\n",
            "Example:\n~~~\n@user #123\n~~~\n",
            id="tilde-fenced-code-block-preserved",
        ),
        pytest.param(
            "Run `@pytest.mark.skip` to skip",
            "Run `@pytest.mark.skip` to skip",
            id="inline-code-preserved",
        ),
        pytest.param(
            "Use ``#include <stdio.h>`` here",
            "Use ``#include <stdio.h>`` here",
            id="double-backtick-inline-code-preserved",
        ),
        pytest.param(
            "Contact user@example.com for help",
            "Contact user@example.com for help",
            id="email-address-preserved",
        ),
        pytest.param(
            "user.name+tag@sub.example.com",
            "user.name+tag@sub.example.com",
            id="complex-email-preserved",
        ),
        pytest.param(
            "See https://docs.example.com/page#section",
            "See https://docs.example.com/page#section",
            id="url-fragment-preserved",
        ),
        pytest.param(
            "Use &#8203; for spacing",
            "Use &#8203; for spacing",
            id="html-entity-preserved",
        ),
        pytest.param(
            "# Heading\n\n## Subheading\n\nText",
            "# Heading\n\n## Subheading\n\nText",
            id="markdown-headings-preserved",
        ),
        pytest.param("", "", id="empty-string"),
        pytest.param(
            "No mentions or issues here.",
            "No mentions or issues here.",
            id="no-sanitization-needed",
        ),
        pytest.param(
            "Thanks @alice!\n```\n@bob #99\n```\nAnd @charlie",
            f"Thanks @{ZWS}alice!\n```\n@bob #99\n```\nAnd @{ZWS}charlie",
            id="mixed-code-and-prose",
        ),
        pytest.param(
            "https://github.com/org/repo/compare/@v1.0...@v2.0",
            "https://redirect.github.com/org/repo/compare/@v1.0...@v2.0",
            id="comparison-url-at-signs-in-path",
        ),
    ],
)
def test_sanitize_markdown_mentions(raw, expected):
    """Neutralize mentions and issue refs while preserving code blocks."""
    assert sanitize_markdown_mentions(raw) == expected


def test_format_release_notes_sanitizes_mentions():
    """Release notes with @mentions are sanitized in output."""
    body = (
        "## What's Changed\n"
        "* Feature by @contributor in #42\n"
        "\n"
        "**New Contributors**\n"
        "* @contributor made their first contribution in"
        " https://github.com/org/repo/pull/42"
    )
    notes = {
        "pkg": ("https://github.com/owner/pkg", [("v1.0", body)]),
    }
    result = format_release_notes(notes)
    # Mentions sanitized.
    assert f"@{ZWS}contributor" in result
    # Issue refs sanitized.
    assert f"#{ZWS}42" in result
    # GitHub URLs rewritten.
    assert "redirect.github.com/org/repo/pull" in result
    assert "https://github.com/org/" not in result


def test_format_release_notes_sanitizes_before_truncation():
    """Truncated release notes are also sanitized."""
    body = "Thanks @alice!\n" + "x" * (RELEASE_NOTES_MAX_LENGTH + 500)
    notes = {
        "pkg": ("https://github.com/owner/pkg", [("v1.0", body)]),
    }
    result = format_release_notes(notes)
    assert f"@{ZWS}alice" in result
    assert "Full release notes" in result
