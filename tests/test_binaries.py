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

"""Tests for the download, verification and cache of binary tools."""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from contextlib import contextmanager
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest
from extra_platforms import AARCH64, LINUX, MACOS, UBUNTU, X86_64

from repomatic.cache import cached_binary_path
from repomatic.tooling.binaries import (
    _STAGING_DIRS,
    ChecksumMismatchError,
    _download_and_verify,
    _extract_binary,
    ensure_binary,
    install_binary,
)
from repomatic.tooling.registry import (
    TOOL_REGISTRY,
    ArchiveFormat,
    BinaryInstallError,
    BinarySpec,
    ResolvedBinary,
    ToolSpec,
    UnsupportedPlatformError,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Download and verification
# ---------------------------------------------------------------------------


def _urlopen_response(content: bytes, *, advertised_length: int | None = -1):
    """Build a `urlopen` double delivering *content* in one read.

    :param content: The body bytes, served before the terminating empty read.
    :param advertised_length: What `Content-Length` reports. The default
        derives it from *content*; `None` omits the header, which sends the
        download path down its indeterminate-progress branch; any other value
        is advertised verbatim, which is how a truncated transfer is staged.
    """
    response = MagicMock()
    length = len(content) if advertised_length == -1 else advertised_length
    response.headers.get = MagicMock(
        return_value=None if length is None else str(length)
    )
    response.read = MagicMock(side_effect=[content, b""])
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


def test_download_and_verify_success(tmp_path):
    """Successful download with matching checksum writes the file."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    with patch("repomatic.http.urlopen", return_value=_urlopen_response(content)):
        _download_and_verify("https://example.com/file", expected, dest)

    assert dest.read_bytes() == content


def test_download_and_verify_no_content_length(tmp_path):
    """A response without Content-Length downloads via the indeterminate Spinner."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    with patch(
        "repomatic.http.urlopen",
        return_value=_urlopen_response(content, advertised_length=None),
    ):
        _download_and_verify("https://example.com/file", expected, dest)

    assert dest.read_bytes() == content


def test_download_and_verify_mismatch(tmp_path):
    """A checksum mismatch raises its own error and removes the download."""
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.http.urlopen",
            return_value=_urlopen_response(b"hello binary world"),
        ),
        pytest.raises(ChecksumMismatchError, match="SHA-256 mismatch"),
    ):
        _download_and_verify("https://example.com/file", "bad" * 16, dest)

    assert not dest.exists()


def test_download_and_verify_retries_transient_failure(tmp_path):
    """A transient network failure is retried and the next attempt succeeds."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[
                URLError("certificate verify failed: self-signed certificate"),
                _urlopen_response(content),
            ],
        ) as fake_urlopen,
        patch("repomatic.http.time.sleep") as fake_sleep,
    ):
        _download_and_verify("https://example.com/file", expected, dest)

    assert fake_urlopen.call_count == 2
    assert fake_sleep.call_count == 1
    assert dest.read_bytes() == content


def test_download_and_verify_gives_up_after_attempts(tmp_path):
    """A persistent network failure surfaces after the last attempt."""
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.http.urlopen", side_effect=URLError("connection reset")
        ) as fake_urlopen,
        patch("repomatic.http.time.sleep"),
        pytest.raises(BinaryInstallError, match="connection reset"),
    ):
        _download_and_verify("https://example.com/file", "0" * 64, dest)

    assert fake_urlopen.call_count == 3
    assert not dest.exists()


@pytest.mark.parametrize("status", [403, 404])
def test_download_and_verify_does_not_retry_a_client_error(tmp_path, status):
    """A client error gets the same answer every time, so it fails at once."""
    dest = tmp_path / "downloaded"
    error = HTTPError(
        "https://example.com/file", status, "Not Found", Message(), io.BytesIO(b"")
    )

    with (
        patch("repomatic.http.urlopen", side_effect=error) as fake_urlopen,
        patch("repomatic.http.time.sleep") as fake_sleep,
        pytest.raises(BinaryInstallError, match=str(status)),
    ):
        _download_and_verify("https://example.com/file", "0" * 64, dest)

    assert fake_urlopen.call_count == 1
    fake_sleep.assert_not_called()


def test_download_and_verify_retries_a_server_error(tmp_path):
    """A server error is transient, so the download is tried again."""
    content = b"hello binary world"
    error = HTTPError(
        "https://example.com/file", 503, "Unavailable", Message(), io.BytesIO(b"")
    )

    with (
        patch(
            "repomatic.http.urlopen", side_effect=[error, _urlopen_response(content)]
        ) as fake_urlopen,
        patch("repomatic.http.time.sleep"),
    ):
        _download_and_verify(
            "https://example.com/file",
            hashlib.sha256(content).hexdigest(),
            tmp_path / "downloaded",
        )

    assert fake_urlopen.call_count == 2


def test_download_and_verify_truncated(tmp_path):
    """A body shorter than Content-Length fails as truncated, not as a mismatch.

    A truncated transfer (proxy hiccup, dropped connection) hashes to a wrong
    digest, which would read as a stale pin or a tampered artifact when the
    registry checksum is correct. Truncation is retried like any network
    failure, so the error only surfaces once every attempt came up short.
    """
    content = b"hello binary world"
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.http.urlopen",
            # Advertise more bytes than the body delivers, on every attempt.
            side_effect=[
                _urlopen_response(content, advertised_length=len(content) + 7)
                for _ in range(3)
            ],
        ),
        patch("repomatic.http.time.sleep"),
        pytest.raises(
            BinaryInstallError, match=r"Truncated download .* got 18 of 25 bytes"
        ),
    ):
        _download_and_verify(
            "https://example.com/file",
            hashlib.sha256(content).hexdigest(),
            dest,
        )

    assert not dest.exists()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _resolved(
    executable: str,
    archive_format: ArchiveFormat,
    strip_components: int = 0,
) -> ResolvedBinary:
    """A download of one platform, for the extraction tests."""
    return ResolvedBinary(
        key=(LINUX, X86_64),
        url="https://example.com/1.0.0/tool",
        checksum="a" * 64,
        archive_format=archive_format,
        strip_components=strip_components,
        executable=executable,
    )


def _create_tar(tmp_path, member_name, mode="w:gz", content=b"#!/bin/sh\necho hi"):
    """Create a tar archive with a single member."""
    archive_path = tmp_path / "tool.tar"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    archive_path.write_bytes(buf.getvalue())
    return archive_path


def _create_zip(tmp_path, member_name, content=b"MZ fake exe"):
    """Create a ZIP archive with a single member."""
    archive_path = tmp_path / "tool.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr(member_name, content)
    return archive_path


def test_extract_binary_raw(tmp_path):
    """RAW format renames the archive to the executable name."""
    archive = tmp_path / "biome-linux-x64"
    archive.write_bytes(b"\x7fELF fake binary")

    result = _extract_binary(archive, _resolved("biome", ArchiveFormat.RAW), tmp_path)

    assert result == tmp_path / "biome"
    assert result.stat().st_mode & 0o755


@pytest.mark.parametrize(
    ("archive_format", "mode"),
    [(ArchiveFormat.TAR_GZ, "w:gz"), (ArchiveFormat.TAR_XZ, "w:xz")],
)
def test_extract_binary_tar(tmp_path, archive_format, mode):
    """A tar archive yields the named executable, made executable."""
    archive = _create_tar(tmp_path, "actionlint", mode=mode)

    result = _extract_binary(archive, _resolved("actionlint", archive_format), tmp_path)

    assert result == tmp_path / "actionlint"
    assert result.stat().st_mode & 0o755


def test_extract_binary_tar_with_strip_components(tmp_path):
    """Leading path components are stripped before the executable is matched."""
    archive = _create_tar(tmp_path, "subdir/bin/mytool")

    result = _extract_binary(
        archive, _resolved("bin/mytool", ArchiveFormat.TAR_GZ, 1), tmp_path
    )

    assert result.name == "mytool"
    assert result.stat().st_mode & 0o755


def test_extract_binary_zip(tmp_path):
    """A ZIP archive yields the executable, `.exe` suffix included."""
    archive = _create_zip(tmp_path, "actionlint.exe")

    result = _extract_binary(
        archive, _resolved("actionlint", ArchiveFormat.ZIP), tmp_path
    )

    assert result == tmp_path / "actionlint.exe"
    assert result.stat().st_mode & 0o755


def test_extract_binary_zip_with_strip_components(tmp_path):
    """Leading path components are stripped in a ZIP archive too."""
    archive = _create_zip(tmp_path, "subdir/bin/mytool.exe")

    result = _extract_binary(
        archive, _resolved("bin/mytool", ArchiveFormat.ZIP, 1), tmp_path
    )

    assert result.name == "mytool.exe"


@pytest.mark.parametrize(
    ("archive_format", "member", "message"),
    [
        (ArchiveFormat.TAR_GZ, "other_binary", "not found in archive"),
        (ArchiveFormat.TAR_GZ, "../../../etc/passwd", "Unsafe archive member"),
        (ArchiveFormat.ZIP, "other.exe", "not found in archive"),
        (ArchiveFormat.ZIP, "../../../etc/passwd", "Unsafe archive member"),
    ],
)
def test_extract_binary_refuses_a_bad_archive(
    tmp_path, archive_format, member, message
):
    """A missing executable or a traversing path is an install failure."""
    if archive_format is ArchiveFormat.ZIP:
        archive = _create_zip(tmp_path, member)
    else:
        archive = _create_tar(tmp_path, member)
    wanted = member if "passwd" in member else "papaya"

    with pytest.raises(BinaryInstallError, match=message):
        _extract_binary(archive, _resolved(wanted, archive_format), tmp_path)


def test_extract_binary_refuses_a_corrupt_archive(tmp_path):
    """An archive that cannot be read is an install failure, not a traceback."""
    archive = tmp_path / "tool.tar"
    archive.write_bytes(b"not a tarball")

    with pytest.raises(BinaryInstallError, match="Cannot extract"):
        _extract_binary(archive, _resolved("papaya", ArchiveFormat.TAR_GZ), tmp_path)


# ---------------------------------------------------------------------------
# install_binary
# ---------------------------------------------------------------------------


TARBALL_URL = "https://example.com/{version}/tool.tar.gz"
"""Download URL for the tests that go through the extract step."""


def _binary_spec(
    *,
    version: str = "1.0.0",
    checksum: str = "a" * 64,
    url: str = "https://example.com/{version}/tool",
    archive_executable: str | None = None,
    archive_format: ArchiveFormat = ArchiveFormat.RAW,
) -> ToolSpec:
    """A single-platform `ToolSpec` for the Linux x86_64 cache tests.

    Every caller pairs it with `_on_linux_x86_64`, so one URL key is enough:
    resolution never reaches a second.
    """
    return ToolSpec(
        name="testtool",
        version=version,
        binary=BinarySpec(
            urls={(LINUX, X86_64): url},
            checksums={(LINUX, X86_64): checksum},
            archive_format=archive_format,
            archive_executable=archive_executable,
        ),
    )


@contextmanager
def _on_linux_x86_64():
    """Pin platform detection to Linux x86_64 for the duration of the block.

    `UBUNTU` rather than `LINUX` on purpose: reporting a real distribution is
    what sends resolution through the group-membership pass, the way an actual
    runner does.
    """
    with (
        patch("repomatic.tooling.registry.current_platform", return_value=UBUNTU),
        patch("repomatic.tooling.registry.current_architecture", return_value=X86_64),
    ):
        yield


@contextmanager
def _staged_download(tmp_path, content: bytes):
    """Stand in for the download and the extraction of an install.

    :return: The mock of the extraction, which hands back a staged executable
        holding *content*.
    """
    extracted = tmp_path / "extracted" / "testtool"
    extracted.parent.mkdir()
    extracted.write_bytes(content)
    extracted.chmod(0o755)
    with (
        patch("repomatic.tooling.binaries._download_and_verify") as mock_download,
        patch(
            "repomatic.tooling.binaries._extract_binary", return_value=extracted
        ) as mock_extract,
    ):
        yield mock_download, mock_extract


def _seed_cache(name: str, content: bytes, *, sidecar: bytes | None = None) -> Path:
    """Put a binary and its digest sidecar in the cache.

    :param sidecar: Content the sidecar digest is computed from. Defaults to
        *content*, which makes the entry verify.
    """
    path = cached_binary_path("testtool", "1.0.0", "linux-x86_64", name)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    path.chmod(0o755)
    digest = hashlib.sha256(content if sidecar is None else sidecar).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(digest, encoding="UTF-8")
    return path


def test_install_binary_missing_platform():
    """A platform with no binary is an install failure of its own kind."""
    spec = ToolSpec(
        name="testtool",
        version="1.0.0",
        binary=BinarySpec(
            urls={(LINUX, AARCH64): "https://example.com/{version}/tool"},
            checksums={(LINUX, AARCH64): "a" * 64},
            archive_format=ArchiveFormat.RAW,
        ),
    )
    with (
        patch("repomatic.tooling.registry.current_platform", return_value=MACOS),
        patch("repomatic.tooling.registry.current_architecture", return_value=X86_64),
        pytest.raises(UnsupportedPlatformError, match="No binary for"),
    ):
        install_binary(spec)


def test_install_binary_refuses_a_platform_with_no_checksum(cache_env):
    """A URL with no recorded digest fails closed before any download."""
    spec = ToolSpec(
        name="testtool",
        version="1.0.0",
        binary=BinarySpec(
            urls={(LINUX, X86_64): "https://example.com/{version}/tool"},
            checksums={},
            archive_format=ArchiveFormat.RAW,
        ),
    )
    with (
        _on_linux_x86_64(),
        pytest.raises(BinaryInstallError, match="No SHA-256 recorded"),
    ):
        install_binary(spec)


def test_install_binary_cache_hit(cache_env):
    """A cached binary whose sidecar matches is used as is."""
    cache_path = _seed_cache("testtool", b"cached-binary-content")

    with _on_linux_x86_64():
        result = install_binary(_binary_spec(checksum="archive-checksum-not-used"))

    assert result == cache_path


@pytest.mark.parametrize(
    "cached_name",
    (
        # What `store_binary` records on POSIX: the extracted file's own name,
        # with the archive's directory prefix already gone.
        "testtool",
        # What it records on Windows, where the archive carries the suffix.
        "testtool.exe",
    ),
)
def test_install_binary_cache_hit_on_a_nested_archive_executable(
    cache_env, cached_name
):
    """A cached entry is found whatever path the executable held in the archive.

    `archive_executable` names a location *inside* the archive, while the cache
    keys an entry on the extracted file's own name. Probing with the nested path
    matched nothing, so a tool declaring one re-downloaded on every call.
    """
    cache_path = _seed_cache(cached_name, b"cached-binary-content")
    spec = _binary_spec(
        archive_executable="bin/testtool", archive_format=ArchiveFormat.ZIP
    )

    with _on_linux_x86_64():
        result = install_binary(spec)

    assert result == cache_path


def test_install_binary_cache_miss_stores(tmp_path, cache_env):
    """A download lands in the cache with its digest sidecar, staging removed."""
    content = b"downloaded-binary"
    spec = _binary_spec(version="2.0.0", url=TARBALL_URL)

    with _on_linux_x86_64(), _staged_download(tmp_path, content) as (download, _):
        result = install_binary(spec)

    expected = cached_binary_path("testtool", "2.0.0", "linux-x86_64", "testtool")
    assert result == expected
    assert expected.read_bytes() == content
    sidecar = expected.with_suffix(expected.suffix + ".sha256")
    assert sidecar.read_text(encoding="UTF-8") == hashlib.sha256(content).hexdigest()
    # The binary is in the cache, so its staging directory is gone already.
    staged_archive = download.call_args.args[2]
    assert not staged_archive.parent.exists()


def test_install_binary_no_cache_flag(tmp_path, cache_env):
    """`no_cache` bypasses the cache and keeps the staging copy alive."""
    with (
        _on_linux_x86_64(),
        _staged_download(tmp_path, b"binary") as (download, extract),
        patch("repomatic.tooling.binaries.store_binary") as mock_store,
    ):
        result = install_binary(_binary_spec(url=TARBALL_URL), no_cache=True)

    mock_store.assert_not_called()
    assert result == extract.return_value
    # The caller is about to run the staging copy, so its directory stays.
    assert download.call_args.args[2].parent.exists()


def test_install_binary_cache_integrity_failure(tmp_path, cache_env):
    """A cached binary that fails its sidecar check is downloaded again."""
    _seed_cache("testtool", b"tampered-content", sidecar=b"original-content")

    with _on_linux_x86_64(), _staged_download(tmp_path, b"real-binary"):
        result = install_binary(_binary_spec(url=TARBALL_URL))

    assert result == cached_binary_path("testtool", "1.0.0", "linux-x86_64", "testtool")
    assert result.read_bytes() == b"real-binary"


def test_install_binary_cache_store_fallback(tmp_path, cache_env):
    """An install the cache loses falls back to the staging copy."""

    def fake_store(*args, **kwargs):
        """Return a cache path that does not exist on disk."""
        return cache_env / "bin" / "ghost" / "binary"

    with (
        _on_linux_x86_64(),
        _staged_download(tmp_path, b"downloaded-binary") as (_, extract),
        patch("repomatic.tooling.binaries.store_binary", side_effect=fake_store),
    ):
        result = install_binary(_binary_spec(url=TARBALL_URL))

    assert result == extract.return_value
    assert result.read_bytes() == b"downloaded-binary"


def test_install_binary_cleans_staging_after_a_failed_download(tmp_path, cache_env):
    """A failed download leaves no staging directory behind."""
    staging_count = len(_STAGING_DIRS)
    with (
        _on_linux_x86_64(),
        _staged_download(tmp_path, b"binary") as (download, _),
    ):
        download.side_effect = ChecksumMismatchError("SHA-256 mismatch")
        with pytest.raises(ChecksumMismatchError):
            install_binary(_binary_spec(url=TARBALL_URL))

    assert not download.call_args.args[2].parent.exists()
    assert len(_STAGING_DIRS) == staging_count


# ---------------------------------------------------------------------------
# ensure_binary
# ---------------------------------------------------------------------------


@patch("repomatic.tooling.binaries.install_binary")
def test_ensure_binary_memoizes_per_tool(mock_install_binary, tmp_path):
    """Repeated calls install once and return the same path.

    Callers in a loop (`format-images` optimizing one PNG per call) must not
    pay the install-and-verify path once per file.
    """
    cached = tmp_path / "labelmaker"
    mock_install_binary.return_value = cached

    assert ensure_binary("labelmaker") == cached
    assert ensure_binary("labelmaker") == cached
    assert mock_install_binary.call_count == 1
    assert mock_install_binary.call_args.args == (TOOL_REGISTRY["labelmaker"],)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        # An unregistered name fails loudly rather than falling back to $PATH.
        pytest.param("papaya", "Unknown tool", id="unknown"),
        # ruff installs through uvx: it has no executable path to hand out.
        pytest.param("ruff", "not a binary tool", id="not-a-binary"),
    ],
)
def test_ensure_binary_rejects_a_tool_without_binary(name, message):
    """Only a registry binary has an executable to hand out."""
    with pytest.raises(ValueError, match=message):
        ensure_binary(name)
