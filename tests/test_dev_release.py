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

"""Tests for `repomatic.github.dev_release`: the rolling dev pre-release sync."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, call, patch

import pytest

from repomatic.github.dev_release import (
    RELEASE_LIST_LIMIT,
    _delete_release_assets,
    cleanup_dev_releases,
    delete_release_by_tag,
    sync_dev_release,
    upload_release_assets,
)
from tests.conftest import patch_gh as shared_patch_gh

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Generator

UNRELEASED_CHANGELOG = """\
# Changelog

## [`6.1.1.dev0` (unreleased)](https://github.com/user/repo/compare/v6.1.0...main)

> [!WARNING]
> This version is **not released yet** and is under active development.

- New feature in progress.

## [`6.1.0` (2026-02-26)](https://github.com/user/repo/compare/v6.0.1...v6.1.0)

- Released feature.
"""

RELEASED_ONLY_CHANGELOG = """\
# Changelog

## [`6.1.0` (2026-02-26)](https://github.com/user/repo/compare/v6.0.1...v6.1.0)

- Released feature.

## [`6.0.1` (2026-02-24)](https://github.com/user/repo/compare/v6.0.0...v6.0.1)

- Bug fix.
"""


# --- sync_dev_release() tests ---


@pytest.fixture
def unreleased_changelog(tmp_path):
    """A changelog on disk whose newest section is still unreleased."""
    path = tmp_path / "changelog.md"
    path.write_text(UNRELEASED_CHANGELOG, encoding="UTF-8")
    return path


def _delete_call(tag: str, repo: str = "user/repo") -> list[str]:
    """The `gh release delete` argv the cleanup paths are expected to issue."""
    return ["release", "delete", tag, "--cleanup-tag", "--yes", "--repo", repo]


@contextmanager
def patch_gh(**mock_kwargs: Any) -> Generator[Mock, None, None]:
    """The conftest `patch_gh`, bound to this module's dispatch points.

    The asset upload runs through {mod}`repomatic.github.releases`, so its
    `run_gh_command` is patched with the same mock, keeping one call sequence.
    """
    with (
        shared_patch_gh("repomatic.github.dev_release", **mock_kwargs) as mock_gh,
        patch("repomatic.github.releases.run_gh_command", mock_gh),
    ):
        yield mock_gh


def test_sync_dev_release_dry_run(unreleased_changelog):
    """Dry-run reports without calling gh."""
    with patch_gh() as mock_gh:
        result = sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=True,
        )

    assert result is True
    mock_gh.assert_not_called()


def _list_call(repo: str = "user/repo") -> list[str]:
    """The `gh release list` argv the dev release paths are expected to issue."""
    return [
        "release",
        "list",
        "--limit",
        str(RELEASE_LIST_LIMIT),
        "--json",
        "tagName",
        "--repo",
        repo,
    ]


def test_sync_dev_release_live(unreleased_changelog):
    """Live mode creates a new draft pre-release when none is listed."""
    with (
        patch_gh(
            side_effect=[
                json.dumps([]),  # List releases.
                None,  # Create new release.
            ],
        ) as mock_gh,
        patch("repomatic.github.dev_release.edit_release_notes") as mock_edit,
    ):
        result = sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=False,
        )

    assert result is True
    assert mock_gh.call_args_list[0] == call(_list_call())
    # A release the list does not show is created, never edited first.
    mock_edit.assert_not_called()
    create_call = mock_gh.call_args_list[1]
    assert create_call[0][0][:3] == ["release", "create", "v6.1.1.dev0"]
    assert "--draft" in create_call[0][0]
    assert "--prerelease" in create_call[0][0]
    assert "--target" in create_call[0][0]


def test_sync_dev_release_never_duplicates_on_a_failed_edit(unreleased_changelog):
    """A listed release whose edit fails is reported, never created again.

    A draft carries no tag, so GitHub accepts a second draft of the same name:
    falling back to a creation would publish two dev pre-releases.
    """
    with (
        patch_gh(
            side_effect=[json.dumps([{"tagName": "v6.1.1.dev0"}])],
        ) as mock_gh,
        patch(
            "repomatic.github.dev_release.edit_release_notes",
            return_value=False,
        ),
        pytest.raises(RuntimeError, match="Could not edit"),
    ):
        sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=False,
        )

    assert mock_gh.call_count == 1


def test_sync_dev_release_refuses_to_create_blind(unreleased_changelog):
    """When the releases cannot be listed, nothing is created."""
    with (
        patch_gh(side_effect=RuntimeError("HTTP 502")) as mock_gh,
        patch("repomatic.github.dev_release.edit_release_notes") as mock_edit,
        pytest.raises(RuntimeError, match="refusing to create"),
    ):
        sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=False,
        )

    assert mock_gh.call_count == 1
    mock_edit.assert_not_called()


def test_sync_dev_release_edits_existing(unreleased_changelog):
    """Edits existing release to preserve assets instead of delete+recreate."""
    # gh release list returns the current dev release.
    release_list = json.dumps([{"tagName": "v6.1.1.dev0"}])
    with (
        patch_gh(
            side_effect=[
                release_list,  # list releases (current kept, not deleted)
            ],
        ) as mock_gh,
        patch(
            "repomatic.github.dev_release.edit_release_notes",
            return_value=True,  # edit succeeds
        ) as mock_edit,
    ):
        result = sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=False,
        )

    assert result is True
    assert mock_gh.call_count == 1
    # Should NOT delete the current dev release.
    delete_calls = [
        c for c in mock_gh.call_args_list if c[0][0][0:2] == ["release", "delete"]
    ]
    assert delete_calls == []
    # Should edit (title refreshed alongside the notes), not create.
    mock_edit.assert_called_once()
    assert mock_edit.call_args.args[:2] == ("v6.1.1.dev0", "user/repo")
    assert mock_edit.call_args.kwargs["title"] == "6.1.1.dev0"
    create_calls = [
        c for c in mock_gh.call_args_list if c[0][0][0:2] == ["release", "create"]
    ]
    assert create_calls == []


def test_sync_dev_release_cleans_stale_releases(unreleased_changelog):
    """Stale dev releases from previous versions are cleaned up."""
    # gh release list returns a stale dev release from a previous version.
    release_list = json.dumps([
        {"tagName": "v6.0.1.dev0"},
        {"tagName": "v6.1.0"},
    ])
    with (
        patch_gh(
            side_effect=[
                release_list,  # list releases
                None,  # delete v6.0.1.dev0 (stale)
                None,  # create new release
            ],
        ) as mock_gh,
        patch("repomatic.github.dev_release.edit_release_notes") as mock_edit,
    ):
        result = sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=False,
        )

    assert result is True
    mock_edit.assert_not_called()
    # Should delete the stale dev release.
    assert mock_gh.call_args_list[1] == call(_delete_call("v6.0.1.dev0"))
    # Should not delete the non-dev release.
    delete_tags = [
        c[0][0][2]
        for c in mock_gh.call_args_list
        if c[0][0][0:2] == ["release", "delete"]
    ]
    assert "v6.1.0" not in delete_tags


def test_sync_dev_release_empty_body(tmp_path):
    """Returns False when changelog section produces an empty body."""
    changelog_path = tmp_path / "changelog.md"
    # Version exists in changelog but has no content.
    changelog_path.write_text(RELEASED_ONLY_CHANGELOG, encoding="UTF-8")

    with patch_gh() as mock_gh:
        result = sync_dev_release(
            changelog_path,
            "9.9.9",
            "user/repo",
            dry_run=False,
        )

    assert result is False
    mock_gh.assert_not_called()


def test_sync_dev_release_body_content(unreleased_changelog):
    """Verifies the release body includes changelog changes."""
    with (
        patch_gh(
            side_effect=[
                json.dumps([]),  # list releases for cleanup
                None,  # create new release
            ],
        ) as mock_gh,
        patch("repomatic.github.dev_release.edit_release_notes") as mock_edit,
    ):
        sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=False,
        )

    mock_edit.assert_not_called()
    # The create call's --notes argument should contain changes.
    create_args = mock_gh.call_args_list[1][0][0]
    notes_idx = create_args.index("--notes")
    body = create_args[notes_idx + 1]
    assert "New feature in progress." in body


# --- cleanup_dev_releases() tests ---


def test_cleanup_dev_releases_deletes_all_dev_tags():
    """Deletes all releases whose tag ends with a `.devN` segment."""
    release_list = json.dumps([
        {"tagName": "v6.2.0.dev0"},
        {"tagName": "v6.1.1.dev1"},
        {"tagName": "v6.1.0"},
        {"tagName": "v6.0.1"},
    ])
    with patch_gh(
        side_effect=[release_list, None, None],
    ) as mock_gh:
        cleanup_dev_releases("user/repo")

    # Should delete both dev releases, not the regular ones.
    assert mock_gh.call_count == 3
    assert mock_gh.call_args_list[0] == call(_list_call())
    assert mock_gh.call_args_list[1] == call(_delete_call("v6.2.0.dev0"))
    assert mock_gh.call_args_list[2] == call(_delete_call("v6.1.1.dev1"))


def test_cleanup_dev_releases_no_dev_releases():
    """No-op when no dev releases exist."""
    release_list = json.dumps([
        {"tagName": "v6.1.0"},
        {"tagName": "v6.0.1"},
    ])
    with patch_gh(
        return_value=release_list,
    ) as mock_gh:
        cleanup_dev_releases("user/repo")

    # Only the list call, no deletions.
    mock_gh.assert_called_once()


def test_cleanup_dev_releases_list_failure():
    """Silently succeeds when release listing fails."""
    with patch_gh(
        side_effect=RuntimeError("API error"),
    ):
        # Should not raise.
        cleanup_dev_releases("user/repo")


def test_cleanup_dev_releases_delete_failure():
    """Continues cleaning when individual deletions fail."""
    release_list = json.dumps([
        {"tagName": "v6.2.0.dev0"},
        {"tagName": "v6.1.1.dev0"},
    ])
    with patch_gh(
        side_effect=[
            release_list,
            RuntimeError("immutable"),  # First delete fails.
            None,  # Second delete succeeds.
        ],
    ) as mock_gh:
        cleanup_dev_releases("user/repo")

    # Should attempt to delete both despite the first failure.
    assert mock_gh.call_count == 3


def test_cleanup_dev_releases_keeps_current_tag():
    """Preserves the current version's dev release when keep_tag is set."""
    release_list = json.dumps([
        {"tagName": "v6.2.0.dev0"},
        {"tagName": "v6.1.1.dev0"},
        {"tagName": "v6.1.0"},
    ])
    with patch_gh(
        side_effect=[release_list, None],
    ) as mock_gh:
        cleanup_dev_releases("user/repo", keep_tag="v6.2.0.dev0")

    # Should only delete the stale dev release, not the kept one.
    assert mock_gh.call_count == 2
    assert mock_gh.call_args_list[1] == call(_delete_call("v6.1.1.dev0"))


# --- delete_release_by_tag() tests ---


def test_delete_release_by_tag_success():
    """Calls gh release delete with the given tag, and reports the deletion."""
    with patch_gh() as mock_gh:
        assert delete_release_by_tag("v6.1.1.dev0", "user/repo") is True

    mock_gh.assert_called_once_with(_delete_call("v6.1.1.dev0"))


def test_delete_release_by_tag_immutable():
    """Silently succeeds for immutable published releases, reporting no deletion."""
    with patch_gh(
        side_effect=RuntimeError("HTTP 422"),
    ):
        # Should not raise.
        assert delete_release_by_tag("v6.1.1.dev0", "user/repo") is False


def test_delete_release_by_tag_leaves_naming_to_the_caller(caplog):
    """The generic helper never calls what it deleted a "dev release"."""
    with patch_gh():
        delete_release_by_tag("v1.2.3", "user/repo")
    assert "dev release" not in caplog.text


# --- _delete_release_assets() tests ---


def test_delete_release_assets_success():
    """Deletes assets and returns count."""
    assets_json = json.dumps({
        "assets": [
            {"apiUrl": "https://api.github.com/repos/user/repo/releases/assets/111"},
            {"apiUrl": "https://api.github.com/repos/user/repo/releases/assets/222"},
        ],
    })
    with patch_gh(
        side_effect=[assets_json, None, None],
    ) as mock_gh:
        result = _delete_release_assets("v6.1.1.dev0", "user/repo")

    assert result == 2
    assert mock_gh.call_args_list[1] == call([
        "api",
        "--method",
        "DELETE",
        "repos/user/repo/releases/assets/111",
    ])
    assert mock_gh.call_args_list[2] == call([
        "api",
        "--method",
        "DELETE",
        "repos/user/repo/releases/assets/222",
    ])


def test_delete_release_assets_no_release():
    """Returns 0 when the release does not exist."""
    with patch_gh(
        side_effect=RuntimeError("not found"),
    ):
        result = _delete_release_assets("v6.1.1.dev0", "user/repo")

    assert result == 0


def test_delete_release_assets_empty():
    """Returns 0 when the release has no assets."""
    with patch_gh(
        return_value=json.dumps({"assets": []}),
    ):
        result = _delete_release_assets("v6.1.1.dev0", "user/repo")

    assert result == 0


def test_delete_release_assets_partial_failure():
    """Continues when individual asset deletions fail."""
    assets_json = json.dumps({
        "assets": [
            {"apiUrl": "https://api.github.com/repos/user/repo/releases/assets/111"},
            {"apiUrl": "https://api.github.com/repos/user/repo/releases/assets/222"},
        ],
    })
    with patch_gh(
        side_effect=[
            assets_json,
            RuntimeError("forbidden"),
            None,
        ],
    ) as mock_gh:
        result = _delete_release_assets("v6.1.1.dev0", "user/repo")

    assert result == 1
    assert mock_gh.call_count == 3


# --- upload_release_assets() tests ---


def test_upload_release_assets_success(tmp_path):
    """Uploads matching files and skips unrelated ones."""
    # Create matching files.
    (tmp_path / "repomatic-6.2.0-linux-x64.bin").touch()
    (tmp_path / "repomatic-6.2.0.tar.gz").touch()
    # Create a non-matching file.
    (tmp_path / "readme.txt").touch()

    assets_json = json.dumps({"assets": []})
    with patch_gh(
        side_effect=[assets_json, None],
    ) as mock_gh:
        result = upload_release_assets("v6.2.0.dev0", "user/repo", tmp_path)

    assert len(result) == 2
    assert all(f.suffix in (".bin", ".gz") for f in result)
    # Last call should be the upload.
    upload_call = mock_gh.call_args_list[-1]
    assert upload_call[0][0][:3] == ["release", "upload", "v6.2.0.dev0"]
    assert "--clobber" in upload_call[0][0]


def test_upload_release_assets_no_files(tmp_path):
    """Returns empty list and makes no gh calls when no matching files."""
    (tmp_path / "readme.txt").touch()

    with patch_gh() as mock_gh:
        result = upload_release_assets("v6.2.0.dev0", "user/repo", tmp_path)

    assert result == []
    mock_gh.assert_not_called()


def test_upload_release_assets_deletes_existing_first(tmp_path):
    """Deletes existing assets before uploading new ones."""
    (tmp_path / "pkg-1.0.0.whl").touch()

    assets_json = json.dumps({
        "assets": [
            {"apiUrl": "https://api.github.com/repos/user/repo/releases/assets/999"},
        ],
    })
    with patch_gh(
        side_effect=[
            assets_json,  # view assets
            None,  # delete asset 999
            None,  # upload
        ],
    ) as mock_gh:
        result = upload_release_assets("v6.2.0.dev0", "user/repo", tmp_path)

    assert len(result) == 1
    # First call: view release assets.
    assert mock_gh.call_args_list[0][0][0][:3] == ["release", "view", "v6.2.0.dev0"]
    # Second call: delete existing asset.
    assert mock_gh.call_args_list[1] == call([
        "api",
        "--method",
        "DELETE",
        "repos/user/repo/releases/assets/999",
    ])
    # Third call: upload.
    assert mock_gh.call_args_list[2][0][0][:3] == ["release", "upload", "v6.2.0.dev0"]


# --- sync_dev_release() with assets tests ---


def test_sync_dev_release_with_assets(unreleased_changelog, tmp_path):
    """End-to-end: metadata sync + asset upload."""
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "repomatic-6.1.1.dev0.tar.gz").touch()

    with (
        patch_gh(
            side_effect=[
                json.dumps([]),  # list releases for cleanup
                None,  # create release
                json.dumps({"assets": []}),  # view assets (empty)
                None,  # upload
            ],
        ) as mock_gh,
        patch("repomatic.github.dev_release.edit_release_notes") as mock_edit,
    ):
        result = sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=False,
            asset_dir=asset_dir,
        )

    assert result is True
    mock_edit.assert_not_called()
    # Last call should be the upload.
    upload_call = mock_gh.call_args_list[-1]
    assert upload_call[0][0][:3] == ["release", "upload", "v6.1.1.dev0"]


def test_sync_dev_release_dry_run_with_assets(unreleased_changelog, tmp_path):
    """Dry-run previews asset files without making gh calls."""
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "repomatic-6.1.1.dev0.whl").touch()
    (asset_dir / "repomatic-6.1.1.dev0.tar.gz").touch()

    with patch_gh() as mock_gh:
        result = sync_dev_release(
            unreleased_changelog,
            "6.1.1.dev0",
            "user/repo",
            dry_run=True,
            asset_dir=asset_dir,
        )

    assert result is True
    mock_gh.assert_not_called()
