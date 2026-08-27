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

"""Tests for the binary tool registry checksum update logic."""

from __future__ import annotations

from unittest.mock import patch

from repomatic.release.checksums import update_registry_checksums
from repomatic.tooling.tool_registry import TOOL_REGISTRY

FAKE_HASH_OLD = "a" * 64
FAKE_HASH_NEW = "b" * 64


def _real_hash(url: str) -> str:
    """Return the checksum the registry already records for *url*.

    Feeding this to the downloader makes every hash match, so a test can single
    out whatever else the sync is supposed to reconcile.
    """
    for spec in TOOL_REGISTRY.values():
        if spec.binary is None:
            continue
        for platform_key, url_template in spec.binary.urls.items():
            if url_template.format(version=spec.version) == url:
                return spec.binary.checksums[platform_key]
    return FAKE_HASH_OLD


def test_update_registry_checksums_replaces_stale_hash(tmp_path):
    """update_registry_checksums replaces stale hashes in tool_registry.py."""
    spec = next(s for s in TOOL_REGISTRY.values() if s.binary is not None)
    assert spec.binary is not None
    old_hash = next(iter(spec.binary.checksums.values()))

    registry = tmp_path / "tool_registry.py"
    registry.write_text(
        f'    checksums={{\n        "linux-x64": "{old_hash}",\n    }},\n',
        encoding="UTF-8",
    )

    with patch(
        "repomatic.release.checksums._download_sha256", return_value=FAKE_HASH_NEW
    ):
        updated = update_registry_checksums(registry)

    # Every binary platform gets a fresh hash because the mock differs.
    assert len(updated) >= 1
    content = registry.read_text(encoding="UTF-8")
    assert old_hash not in content
    assert FAKE_HASH_NEW in content


def test_update_registry_checksums_noop_when_current(tmp_path):
    """update_registry_checksums does not rewrite when all hashes match."""
    registry = tmp_path / "tool_registry.py"
    content = "# no checksums to update\n"
    registry.write_text(content, encoding="UTF-8")

    with patch("repomatic.release.checksums._download_sha256", side_effect=_real_hash):
        updated = update_registry_checksums(registry)

    assert updated == []
    assert registry.read_text(encoding="UTF-8") == content


def test_update_registry_checksums_reconciles_version_stamp(tmp_path):
    """A stale VERSIONS stamp is reconciled to the registry version."""
    name, spec = next((n, s) for n, s in TOOL_REGISTRY.items() if s.binary is not None)
    assert spec.version != "9.9.9"

    registry = tmp_path / "tool_registry.py"
    registry.write_text(
        f'VERSIONS = {{\n    "{name}": "9.9.9",\n}}\n', encoding="UTF-8"
    )

    with patch("repomatic.release.checksums._download_sha256", side_effect=_real_hash):
        update_registry_checksums(registry)

    content = registry.read_text(encoding="UTF-8")
    assert f'"{name}": "{spec.version}"' in content
    assert "9.9.9" not in content


def test_update_registry_checksums_version_override(tmp_path):
    """version_overrides downloads at the override version and stamps it.

    Models the `sync-tool-versions` flow: the source was bumped (so the file
    holds the new version) while the in-memory registry still holds the old
    one, and the override drives both the download URL and the VERSIONS stamp.
    """
    name, spec = next((n, s) for n, s in TOOL_REGISTRY.items() if s.binary is not None)
    assert spec.binary is not None
    new_version = "99.0.0"
    old_hash = next(iter(spec.binary.checksums.values()))

    registry = tmp_path / "tool_registry.py"
    registry.write_text(
        f'VERSIONS = {{\n    "{name}": "9.9.9",\n}}\n# {old_hash}\n',
        encoding="UTF-8",
    )

    seen_urls: list[str] = []

    def capture(url):
        seen_urls.append(url)
        return FAKE_HASH_NEW

    with patch("repomatic.release.checksums._download_sha256", side_effect=capture):
        update_registry_checksums(registry, version_overrides={name: new_version})

    content = registry.read_text(encoding="UTF-8")
    # The stamp is reconciled to the override, not the in-memory version.
    assert f'"{name}": "{new_version}"' in content
    # The override version drove the download URL.
    override_url = next(iter(spec.binary.urls.values())).format(version=new_version)
    assert override_url in seen_urls
