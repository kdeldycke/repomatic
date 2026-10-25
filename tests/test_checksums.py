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

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.github.gh import GhCommandError
from repomatic.http import FetchError
from repomatic.tooling.checksums import (
    AttestationError,
    _download_sha256,
    checksum_targets,
    update_registry_checksums,
    verify_attestation,
)
from repomatic.tooling.registry import LINUX, TOOL_REGISTRY, X86_64

FAKE_HASH_OLD = "a" * 64
FAKE_HASH_NEW = "b" * 64

FIG_URL = "https://github.com/orchard/fig/releases/download/v1.0.0/fig_linux_amd64"
"""A release artifact whose checksum a stubbed recompute reports as stale."""


def _real_hash(url: str, verify=None) -> str:
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


def test_download_sha256_retries_a_failed_first_attempt():
    """One connection reset does not end a download: the next attempt answers.

    A batch downloads every binary of the registry, so one transient error must
    not abort it.
    """
    content = b"pear tart"
    response = MagicMock()
    response.headers.get.return_value = str(len(content))
    response.read.side_effect = [content, b""]
    response.__enter__.return_value = response
    response.__exit__.return_value = False

    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[ConnectionResetError("Connection reset by peer"), response],
        ) as fake_urlopen,
        patch("repomatic.http.time.sleep") as fake_sleep,
    ):
        digest = _download_sha256("https://example.com/pear-tart.tar.gz")

    assert digest == hashlib.sha256(content).hexdigest()
    assert fake_urlopen.call_count == 2
    assert fake_sleep.call_count == 1


def test_update_registry_checksums_replaces_stale_hash(tmp_path):
    """update_registry_checksums replaces stale hashes in tooling/registry.py."""
    spec = next(s for s in TOOL_REGISTRY.values() if s.binary is not None)
    assert spec.binary is not None
    old_hash = next(iter(spec.binary.checksums.values()))

    registry = tmp_path / "registry.py"
    registry.write_text(
        f'    checksums={{\n        "linux-x64": "{old_hash}",\n    }},\n',
        encoding="UTF-8",
    )

    with patch(
        "repomatic.tooling.checksums._download_sha256", return_value=FAKE_HASH_NEW
    ):
        updated = update_registry_checksums(registry)

    # Every binary platform gets a fresh hash because the mock differs.
    assert len(updated) >= 1
    content = registry.read_text(encoding="UTF-8")
    assert old_hash not in content
    assert FAKE_HASH_NEW in content


@pytest.mark.parametrize(
    ("outcome", "exit_code", "message"),
    (
        pytest.param(
            {"return_value": [(FIG_URL, FAKE_HASH_OLD, FAKE_HASH_NEW)]},
            0,
            f"Updated: {FIG_URL}\n  Old: {FAKE_HASH_OLD}\n  New: {FAKE_HASH_NEW}\n",
            id="stale",
        ),
        pytest.param(
            {"return_value": []}, 0, "All checksums are up to date.", id="current"
        ),
        pytest.param(
            {"side_effect": FetchError("HTTP 503")},
            1,
            "Cannot download a release artifact: HTTP 503",
            id="unreachable",
        ),
        pytest.param(
            {"side_effect": AttestationError("fig did not verify")},
            1,
            "fig did not verify",
            id="unattested",
        ),
    ),
)
def test_update_checksums_reports_each_outcome(outcome, exit_code, message):
    """The command lists each rewritten hash, or ends on the error that stopped it.

    The recompute is stubbed: the real one rewrites the installed registry.
    """
    with patch("repomatic.cli.tools.update_registry_checksums", **outcome) as update:
        result = CliRunner().invoke(
            repomatic, ["--verbosity", "INFO", "update-checksums"]
        )

    assert result.exit_code == exit_code, result.output
    assert message in result.output
    update.assert_called_once()


def test_update_registry_checksums_noop_when_current(tmp_path):
    """update_registry_checksums does not rewrite when all hashes match."""
    registry = tmp_path / "registry.py"
    content = "# no checksums to update\n"
    registry.write_text(content, encoding="UTF-8")

    with patch("repomatic.tooling.checksums._download_sha256", side_effect=_real_hash):
        updated = update_registry_checksums(registry)

    assert updated == []
    assert registry.read_text(encoding="UTF-8") == content


def test_update_registry_checksums_reconciles_version_stamp(tmp_path):
    """A stale VERSIONS stamp is reconciled to the registry version."""
    name, spec = next((n, s) for n, s in TOOL_REGISTRY.items() if s.binary is not None)
    assert spec.version != "9.9.9"

    registry = tmp_path / "registry.py"
    registry.write_text(
        f'VERSIONS = {{\n    "{name}": "9.9.9",\n}}\n', encoding="UTF-8"
    )

    with patch("repomatic.tooling.checksums._download_sha256", side_effect=_real_hash):
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

    registry = tmp_path / "registry.py"
    registry.write_text(
        f'VERSIONS = {{\n    "{name}": "9.9.9",\n}}\n# {old_hash}\n',
        encoding="UTF-8",
    )

    seen_urls: list[str] = []

    def capture(url, verify=None):
        seen_urls.append(url)
        return FAKE_HASH_NEW

    with patch("repomatic.tooling.checksums._download_sha256", side_effect=capture):
        update_registry_checksums(registry, version_overrides={name: new_version})

    content = registry.read_text(encoding="UTF-8")
    # The stamp is reconciled to the override, not the in-memory version.
    assert f'"{name}": "{new_version}"' in content
    # The override version drove the download URL.
    override_url = next(iter(spec.binary.urls.values())).format(version=new_version)
    assert override_url in seen_urls


def test_update_registry_checksums_override_limits_the_run_to_its_tools(tmp_path):
    """An override verifies the bumped tool alone, and stamps no other tool.

    A version bump touches one or two tools: downloading the artifacts of every
    binary tool for it multiplied the run several times over. A tool left out
    keeps the stamp of its pinned version, which its checksums still match.
    """
    bumped, other = [n for n, s in TOOL_REGISTRY.items() if s.binary is not None][:2]
    bumped_spec = TOOL_REGISTRY[bumped]
    assert bumped_spec.binary is not None

    registry = tmp_path / "registry.py"
    registry.write_text(
        f'VERSIONS = {{\n    "{bumped}": "1.0.0",\n    "{other}": "1.0.0",\n}}\n',
        encoding="UTF-8",
    )

    seen_urls: list[str] = []

    def capture(url, verify=None):
        seen_urls.append(url)
        return FAKE_HASH_NEW

    with patch("repomatic.tooling.checksums._download_sha256", side_effect=capture):
        update_registry_checksums(registry, version_overrides={bumped: "99.0.0"})

    assert len(seen_urls) == len(bumped_spec.binary.urls)
    assert all("99.0.0" in url for url in seen_urls)
    content = registry.read_text(encoding="UTF-8")
    assert f'"{bumped}": "99.0.0"' in content
    assert f'"{other}": "1.0.0"' in content


def test_checksum_targets_cover_every_binary_platform():
    """With no override, every platform of every binary tool is verified."""
    expected = sum(
        len(spec.binary.urls)
        for spec in TOOL_REGISTRY.values()
        if spec.binary is not None
    )
    targets = checksum_targets()
    assert len(targets) == expected
    for spec, resolved in targets:
        assert spec.version in resolved.url
        assert resolved.checksum is not None


def _fake_download(content: bytes):
    """A `download` stand-in writing *content* and returning its digest."""

    def fake(url, path):
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    return fake


def test_download_sha256_hands_the_named_asset_to_the_verifier():
    """The verifier reads the download before the scratch directory goes."""
    seen = []
    url = "https://github.com/orchard/fig/releases/download/v1.0.0/fig_linux_amd64"
    with patch(
        "repomatic.tooling.checksums.download", side_effect=_fake_download(b"fig")
    ):
        digest = _download_sha256(url, lambda path: seen.append(path.read_bytes()))
    assert digest == hashlib.sha256(b"fig").hexdigest()
    assert seen == [b"fig"]


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        pytest.param(
            "actionlint",
            ["attestation", "verify", "{path}", "--repo", "rhysd/actionlint"],
            id="build-provenance",
        ),
        pytest.param(
            "shfmt",
            ["release", "verify-asset", "v{version}", "{path}", "--repo", "mvdan/sh"],
            id="immutable-release",
        ),
    ],
)
def test_verify_attestation_runs_the_check_upstream_supports(tool, expected, tmp_path):
    """Each attestation level is verified by the `gh` command that reads it."""
    spec = TOOL_REGISTRY[tool]
    resolved = spec.resolve_binary((LINUX, X86_64))
    path = tmp_path / resolved.archive_name
    with patch("repomatic.tooling.checksums.run_gh_command") as gh:
        verify_attestation(resolved, path)
    assert gh.call_args[0][0] == [
        arg.format(path=path, version=spec.version) for arg in expected
    ]


def test_a_failed_attestation_aborts_the_run_and_rewrites_nothing(tmp_path):
    """A download its attestation does not cover never reaches the registry."""
    registry = tmp_path / "registry.py"
    registry.write_text('VERSIONS = {\n    "shfmt": "1.0.0",\n}\n', encoding="UTF-8")
    original = registry.read_text(encoding="UTF-8")
    refusal = GhCommandError("attestation for v99.0.0 does not contain subject")
    with (
        patch(
            "repomatic.tooling.checksums.download",
            side_effect=_fake_download(b"plum"),
        ),
        patch("repomatic.tooling.checksums.run_gh_command", side_effect=refusal),
        pytest.raises(AttestationError, match="did not verify"),
    ):
        update_registry_checksums(registry, version_overrides={"shfmt": "99.0.0"})
    assert registry.read_text(encoding="UTF-8") == original


def test_a_tool_attesting_nothing_is_not_verified(tmp_path):
    """No `gh` call is made for a tool whose upstream publishes no attestation."""
    spec = TOOL_REGISTRY["lychee"]
    assert spec.binary is not None
    assert spec.binary.attestation is None
    registry = tmp_path / "registry.py"
    registry.write_text('VERSIONS = {\n    "lychee": "1.0.0",\n}\n', encoding="UTF-8")
    with (
        patch(
            "repomatic.tooling.checksums.download",
            side_effect=_fake_download(b"kiwi"),
        ),
        patch("repomatic.tooling.checksums.run_gh_command") as gh,
    ):
        update_registry_checksums(registry, version_overrides={"lychee": spec.version})
    gh.assert_not_called()
