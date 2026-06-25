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

"""Tests for SHA-256 checksum update logic."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from repomatic.checksums import (
    _find_checksum_pairs,
    update_checksums,
    update_registry_checksums,
)

FAKE_HASH_OLD = "a" * 64
FAKE_HASH_NEW = "b" * 64


@pytest.mark.parametrize(
    ("description", "lines", "expected_url", "expected_hash_line", "expected_hash"),
    [
        (
            "single-line echo|sha256sum",
            [
                "  curl -fsSL --output /tmp/tool.tar.gz \\",
                '    "https://github.com/org/tool/releases/download/v1.0/tool.tar.gz"',
                f'  echo "{FAKE_HASH_OLD}  /tmp/tool.tar.gz" | sha256sum --check',
            ],
            "https://github.com/org/tool/releases/download/v1.0/tool.tar.gz",
            2,
            FAKE_HASH_OLD,
        ),
        (
            "multi-line echo \\ | sha256sum",
            [
                "  curl -fsSL --output /tmp/tool.tar.gz \\",
                '    "https://github.com/org/tool/releases/download/v1.0/tool.tar.gz"',
                f'  echo "{FAKE_HASH_OLD}  /tmp/tool.tar.gz" \\',
                "    | sha256sum --check",
            ],
            "https://github.com/org/tool/releases/download/v1.0/tool.tar.gz",
            2,
            FAKE_HASH_OLD,
        ),
    ],
)
def test_find_checksum_pairs(
    description, lines, expected_url, expected_hash_line, expected_hash
):
    """Verify _find_checksum_pairs handles both single and multi-line patterns."""
    results = list(_find_checksum_pairs(lines))
    assert len(results) == 1, description
    url, hash_line_idx, old_hash = results[0]
    assert url == expected_url
    assert hash_line_idx == expected_hash_line
    assert old_hash == expected_hash


def test_find_checksum_pairs_no_match():
    """Lines without sha256sum produce no results."""
    lines = [
        "  curl -fsSL --output /tmp/tool.tar.gz \\",
        '    "https://github.com/org/tool/releases/download/v1.0/tool.tar.gz"',
        "  tar xzf /tmp/tool.tar.gz",
    ]
    assert list(_find_checksum_pairs(lines)) == []


def test_update_checksums_replaces_hash(tmp_path):
    """update_checksums downloads and replaces a stale hash."""
    workflow = tmp_path / "test.yaml"
    workflow.write_text(
        "  curl -fsSL --output /tmp/t.tar.gz \\\n"
        '    "https://github.com/org/tool/releases/download/v1.0/t.tar.gz"\n'
        f'  echo "{FAKE_HASH_OLD}  /tmp/t.tar.gz" \\\n'
        "    | sha256sum --check\n",
        encoding="UTF-8",
    )

    with patch("repomatic.checksums._download_sha256", return_value=FAKE_HASH_NEW):
        updated = update_checksums(workflow)

    assert len(updated) == 1
    assert updated[0][1] == FAKE_HASH_OLD
    assert updated[0][2] == FAKE_HASH_NEW
    assert FAKE_HASH_NEW in workflow.read_text(encoding="UTF-8")
    assert FAKE_HASH_OLD not in workflow.read_text(encoding="UTF-8")


def test_update_checksums_noop_when_hash_matches(tmp_path):
    """update_checksums does not rewrite the file when checksums are current."""
    workflow = tmp_path / "test.yaml"
    content = (
        '  curl -fsSL "https://github.com/org/tool/releases/download/v1.0/t.tar.gz"'
        " --output /tmp/t.tar.gz\n"
        f'  echo "{FAKE_HASH_OLD}  /tmp/t.tar.gz" | sha256sum --check\n'
    )
    workflow.write_text(content, encoding="UTF-8")

    with patch("repomatic.checksums._download_sha256", return_value=FAKE_HASH_OLD):
        updated = update_checksums(workflow)

    assert updated == []
    assert workflow.read_text(encoding="UTF-8") == content


# ---------------------------------------------------------------------------
# Registry checksum updates
# ---------------------------------------------------------------------------


def test_update_registry_checksums_replaces_stale_hash(tmp_path):
    """update_registry_checksums replaces stale hashes in Python source."""
    from repomatic.tool_runner import TOOL_REGISTRY

    # Pick the first binary tool's checksum to test replacement.
    binary_spec = None
    for spec in TOOL_REGISTRY.values():
        if spec.binary is not None:
            binary_spec = spec
            break
    assert binary_spec is not None
    assert binary_spec.binary is not None

    old_hash = next(iter(binary_spec.binary.checksums.values()))

    # Write a fake registry file containing the real checksum.
    registry = tmp_path / "tool_runner.py"
    registry.write_text(
        f'    checksums={{\n        "linux-x64": "{old_hash}",\n    }},\n',
        encoding="UTF-8",
    )

    with patch("repomatic.checksums._download_sha256", return_value=FAKE_HASH_NEW):
        updated = update_registry_checksums(registry)

    # All binary tools get updated because mock returns a different hash.
    assert len(updated) >= 1
    content = registry.read_text(encoding="UTF-8")
    # The old checksum from the picked tool should be replaced.
    assert old_hash not in content
    assert FAKE_HASH_NEW in content


def test_update_registry_checksums_noop_when_current(tmp_path):
    """update_registry_checksums does not rewrite when all hashes match."""
    registry = tmp_path / "tool_runner.py"
    content = "# no checksums to update\n"
    registry.write_text(content, encoding="UTF-8")

    # Mock _download_sha256 to return the same hash for all tools.
    def same_hash(url):
        # Return the actual checksum from the registry so nothing changes.
        from repomatic.tool_runner import TOOL_REGISTRY

        for spec in TOOL_REGISTRY.values():
            if spec.binary is None:
                continue
            for platform_key, url_template in spec.binary.urls.items():
                resolved = url_template.format(version=spec.version)
                if resolved == url:
                    return spec.binary.checksums[platform_key]
        return FAKE_HASH_OLD

    with patch("repomatic.checksums._download_sha256", side_effect=same_hash):
        updated = update_registry_checksums(registry)

    assert updated == []
    assert registry.read_text(encoding="UTF-8") == content


def test_update_registry_checksums_reconciles_version_stamp(tmp_path):
    """update_registry_checksums refreshes a stale VERSIONS stamp in the sidecar."""
    from repomatic.tool_runner import TOOL_REGISTRY

    name, spec = next((n, s) for n, s in TOOL_REGISTRY.items() if s.binary is not None)
    assert spec.version != "9.9.9"

    def real_hash(url):
        # Return each tool's real checksum so no hash changes; only the stale
        # version stamp should be reconciled.
        for s in TOOL_REGISTRY.values():
            if s.binary is None:
                continue
            for pk, tmpl in s.binary.urls.items():
                if tmpl.format(version=s.version) == url:
                    return s.binary.checksums[pk]
        return FAKE_HASH_OLD

    sidecar = tmp_path / "tool_checksums.py"
    sidecar.write_text(f'VERSIONS = {{\n    "{name}": "9.9.9",\n}}\n', encoding="UTF-8")

    with patch("repomatic.checksums._download_sha256", side_effect=real_hash):
        update_registry_checksums(sidecar)

    content = sidecar.read_text(encoding="UTF-8")
    assert f'"{name}": "{spec.version}"' in content
    assert "9.9.9" not in content
