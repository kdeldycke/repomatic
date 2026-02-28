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

from unittest.mock import patch

import json

from repomatic.renovate import (
    RenovateCheckResult,
    check_commit_statuses_permission,
    check_dependabot_config_absent,
    check_dependabot_security_disabled,
    check_renovate_config_exists,
    collect_check_results,
    get_dependabot_config_path,
    is_lock_diff_only_timestamp_noise,
    revert_lock_if_noise,
    run_migration_checks,
    sync_uv_lock,
)


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
        passed, msg = check_dependabot_security_disabled("owner/repo")
        # Non-fatal, passes but with warning message.
        assert passed is True


def test_has_permission():
    """Pass when token has permission."""
    with patch("repomatic.renovate.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        passed, msg = check_commit_statuses_permission("owner/repo", "abc123")
        assert passed is True
        assert "access" in msg


def test_no_permission():
    """Pass with warning when no permission (non-fatal)."""
    with patch("repomatic.renovate.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("gh command failed")
        passed, msg = check_commit_statuses_permission("owner/repo", "abc123")
        # Non-fatal, passes but with warning.
        assert passed is True
        assert "permission" in msg.lower()


def test_all_checks_pass(tmp_path, monkeypatch, capsys):
    """Return 0 when all checks pass."""
    monkeypatch.chdir(tmp_path)
    # Create renovate.json5 so that check passes.
    (tmp_path / "renovate.json5").touch()
    with patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec:
        with patch("repomatic.renovate.check_commit_statuses_permission") as mock_perm:
            mock_sec.return_value = (True, "Disabled")
            mock_perm.return_value = (True, "Has access")
            exit_code = run_migration_checks("owner/repo", "abc123")
            assert exit_code == 0


def test_renovate_config_missing(tmp_path, monkeypatch, capsys):
    """Return 1 when renovate.json5 is missing."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec:
        with patch("repomatic.renovate.check_commit_statuses_permission") as mock_perm:
            mock_sec.return_value = (True, "Disabled")
            mock_perm.return_value = (True, "Has access")
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
    with patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec:
        with patch("repomatic.renovate.check_commit_statuses_permission") as mock_perm:
            mock_sec.return_value = (True, "Disabled")
            mock_perm.return_value = (True, "Has access")
            exit_code = run_migration_checks("owner/repo", "abc123")
            assert exit_code == 1
            captured = capsys.readouterr()
            assert "::error::" in captured.out


def test_security_updates_enabled(tmp_path, monkeypatch, capsys):
    """Return 1 when Dependabot security updates are enabled."""
    monkeypatch.chdir(tmp_path)
    # Create renovate.json5 so that check passes.
    (tmp_path / "renovate.json5").touch()
    with patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec:
        with patch("repomatic.renovate.check_commit_statuses_permission") as mock_perm:
            mock_sec.return_value = (False, "Security updates enabled")
            mock_perm.return_value = (True, "Has access")
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


def test_to_pr_body_needs_migration():
    """Generate PR body when migration is needed."""
    result = RenovateCheckResult(
        renovate_config_exists=False,
        dependabot_config_path=".github/dependabot.yaml",
        dependabot_security_disabled=False,
        commit_statuses_permission=True,
        repo="owner/repo",
    )
    body = result.to_pr_body()
    assert "Migrate from Dependabot to Renovate" in body
    assert "Export `renovate.json5`" in body
    assert "Remove `.github/dependabot.yaml`" in body
    assert "🔧 Created by this PR" in body
    assert "🔧 Removed by this PR" in body
    assert "⚠️ Enabled" in body
    assert "https://github.com/owner/repo/settings/security_analysis" in body


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
    with patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec:
        with patch("repomatic.renovate.check_commit_statuses_permission") as mock_perm:
            mock_sec.return_value = (True, "Disabled")
            mock_perm.return_value = (True, "Has access")
            result = collect_check_results("owner/repo", "abc123")
            assert result.renovate_config_exists is True
            assert result.dependabot_config_path == ""
            assert result.dependabot_security_disabled is True
            assert result.commit_statuses_permission is True


def test_with_dependabot_config(tmp_path, monkeypatch):
    """Collect results when Dependabot config exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "renovate.json5").touch()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yaml").touch()
    with patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec:
        with patch("repomatic.renovate.check_commit_statuses_permission") as mock_perm:
            mock_sec.return_value = (True, "Disabled")
            mock_perm.return_value = (True, "Has access")
            result = collect_check_results("owner/repo", "abc123")
            assert result.dependabot_config_path == ".github/dependabot.yaml"


def test_missing_renovate_config(tmp_path, monkeypatch):
    """Collect results when renovate.json5 is missing."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.renovate.check_dependabot_security_disabled") as mock_sec:
        with patch("repomatic.renovate.check_commit_statuses_permission") as mock_perm:
            mock_sec.return_value = (True, "Disabled")
            mock_perm.return_value = (True, "Has access")
            result = collect_check_results("owner/repo", "abc123")
            assert result.renovate_config_exists is False


# Sample diff containing only exclude-newer timestamp noise.
_TIMESTAMP_ONLY_DIFF = """\
diff --git a/uv.lock b/uv.lock
index abc1234..def5678 100644
--- a/uv.lock
+++ b/uv.lock
@@ -5,7 +5,7 @@
-exclude-newer = "2026-02-11T13:52:20.092144Z"
+exclude-newer = "2026-02-11T14:09:32.945450358Z"
@@ -9,3 +9,3 @@
-repomatic = { timestamp = "2026-02-18T13:52:20.092402Z", span = "PT0S" }
+repomatic = { timestamp = "2026-02-18T14:09:32.94545704Z", span = "PT0S" }
"""

# Sample diff with real dependency changes mixed in.
_REAL_CHANGE_DIFF = """\
diff --git a/uv.lock b/uv.lock
index abc1234..def5678 100644
--- a/uv.lock
+++ b/uv.lock
@@ -5,7 +5,7 @@
-exclude-newer = "2026-02-11T13:52:20.092144Z"
+exclude-newer = "2026-02-11T14:09:32.945450358Z"
@@ -9,3 +9,3 @@
-repomatic = { timestamp = "2026-02-18T13:52:20.092402Z", span = "PT0S" }
+repomatic = { timestamp = "2026-02-18T14:09:32.94545704Z", span = "PT0S" }
-version = "1.2.3"
+version = "1.2.4"
"""


def _mock_subprocess_run(stdout):
    """Create a mock for subprocess.run returning the given stdout."""
    mock_result = type("Result", (), {"stdout": stdout, "returncode": 0})()
    return patch("repomatic.renovate.subprocess.run", return_value=mock_result)


def test_timestamp_only_diff_is_noise(tmp_path):
    """Return True when diff contains only timestamp changes."""
    lock_path = tmp_path / "uv.lock"
    with _mock_subprocess_run(_TIMESTAMP_ONLY_DIFF):
        assert is_lock_diff_only_timestamp_noise(lock_path) is True


def test_real_changes_are_not_noise(tmp_path):
    """Return False when diff contains real dependency changes."""
    lock_path = tmp_path / "uv.lock"
    with _mock_subprocess_run(_REAL_CHANGE_DIFF):
        assert is_lock_diff_only_timestamp_noise(lock_path) is False


def test_empty_diff_is_not_noise(tmp_path):
    """Return False when there is no diff output."""
    lock_path = tmp_path / "uv.lock"
    with _mock_subprocess_run(""):
        assert is_lock_diff_only_timestamp_noise(lock_path) is False


def test_revert_lock_if_noise_reverts(tmp_path):
    """Revert lock file when diff is only timestamp noise."""
    lock_path = tmp_path / "uv.lock"
    with patch(
        "repomatic.renovate.is_lock_diff_only_timestamp_noise", return_value=True
    ):
        with patch("repomatic.renovate.subprocess.run") as mock_run:
            result = revert_lock_if_noise(lock_path)
            assert result is True
            mock_run.assert_called_once_with(
                ["git", "checkout", "--", str(lock_path)],
                check=True,
            )


def test_revert_lock_if_noise_keeps(tmp_path):
    """Keep lock file when diff contains real changes."""
    lock_path = tmp_path / "uv.lock"
    with patch(
        "repomatic.renovate.is_lock_diff_only_timestamp_noise", return_value=False
    ):
        result = revert_lock_if_noise(lock_path)
        assert result is False


def test_sync_uv_lock_keeps_real_changes(tmp_path):
    """Keep lock file when real dependency changes exist."""
    lock_path = tmp_path / "uv.lock"
    with patch("repomatic.renovate.subprocess.run") as mock_run:
        with patch(
            "repomatic.renovate.is_lock_diff_only_timestamp_noise",
            return_value=False,
        ):
            reverted = sync_uv_lock(lock_path)
            assert reverted is False
            # uv lock was called.
            mock_run.assert_called_once_with(
                ["uv", "--no-progress", "lock", "--upgrade"], check=True
            )


def test_sync_uv_lock_reverts_noise(tmp_path):
    """Revert lock file when only timestamp noise changed."""
    lock_path = tmp_path / "uv.lock"
    with patch("repomatic.renovate.subprocess.run") as mock_run:
        with patch(
            "repomatic.renovate.is_lock_diff_only_timestamp_noise",
            return_value=True,
        ):
            reverted = sync_uv_lock(lock_path)
            assert reverted is True
            # uv lock + git checkout were called.
            assert mock_run.call_count == 2
