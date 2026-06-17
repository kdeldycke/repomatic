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

"""Tests for PyPI client helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from repomatic.pypi import (
    PYPI_TRUSTED_PUBLISHER_SETTINGS_URL,
    TrustedPublisher,
    get_latest_release_file,
    get_trusted_publishers,
    pypi_trusted_publisher_settings_url,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing_extensions import Self


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = BytesIO(data)

    def read(self) -> bytes:
        return self._data.read()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _patch_pypi_json(
    payload: Mapping[str, object] | None,
) -> AbstractContextManager[MagicMock]:
    """Patch `_fetch_json` to return `payload`."""
    return patch("repomatic.pypi._fetch_json", return_value=payload)


def test_latest_release_file_picks_most_recent_wheel():
    """Pick the wheel from the version with the most recent earliest upload."""
    payload = {
        "releases": {
            "1.0.0": [
                {
                    "filename": "cherries-1.0.0.tar.gz",
                    "upload_time": "2026-01-01T00:00:00",
                },
                {
                    "filename": "cherries-1.0.0-py3-none-any.whl",
                    "upload_time": "2026-01-01T00:00:01",
                },
            ],
            "1.1.0": [
                {
                    "filename": "cherries-1.1.0.tar.gz",
                    "upload_time": "2026-03-01T00:00:00",
                },
                {
                    "filename": "cherries-1.1.0-py3-none-any.whl",
                    "upload_time": "2026-03-01T00:00:01",
                },
            ],
        },
    }
    with _patch_pypi_json(payload):
        result = get_latest_release_file("cherries")
    assert result == ("1.1.0", "cherries-1.1.0-py3-none-any.whl")


def test_latest_release_file_falls_back_to_sdist():
    """Fall back to the sdist when no wheel exists for the latest release."""
    payload = {
        "releases": {
            "0.1.0": [
                {
                    "filename": "cherries-0.1.0.tar.gz",
                    "upload_time": "2026-04-01T00:00:00",
                },
            ],
        },
    }
    with _patch_pypi_json(payload):
        assert get_latest_release_file("cherries") == (
            "0.1.0",
            "cherries-0.1.0.tar.gz",
        )


def test_latest_release_file_skips_yanked_versions():
    """Yanked-only versions do not count as the latest release."""
    payload = {
        "releases": {
            "1.0.0": [
                {
                    "filename": "cherries-1.0.0-py3-none-any.whl",
                    "upload_time": "2026-01-01T00:00:00",
                },
            ],
            "2.0.0": [
                {
                    "filename": "cherries-2.0.0-py3-none-any.whl",
                    "upload_time": "2026-04-01T00:00:00",
                    "yanked": True,
                },
            ],
        },
    }
    with _patch_pypi_json(payload):
        assert get_latest_release_file("cherries") == (
            "1.0.0",
            "cherries-1.0.0-py3-none-any.whl",
        )


def test_latest_release_file_no_releases():
    """Return None when the package has no releases at all."""
    with _patch_pypi_json({"releases": {}}):
        assert get_latest_release_file("cherries") is None


def test_latest_release_file_api_failure():
    """Return None when the metadata fetch itself failed."""
    with _patch_pypi_json(None):
        assert get_latest_release_file("cherries") is None


def test_get_trusted_publishers_match():
    """Parse a single GitHub publisher bundle into a TrustedPublisher tuple."""
    payload = {
        "version": 1,
        "attestation_bundles": [
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": "owner/cherries",
                    "workflow": "release.yaml",
                    "environment": None,
                },
                "attestations": [],
            },
        ],
    }
    body = json.dumps(payload).encode()
    with patch(
        "repomatic.pypi.urlopen",
        return_value=_FakeResponse(body),
    ):
        result = get_trusted_publishers(
            "cherries", "1.2.3", "cherries-1.2.3-py3-none-any.whl"
        )
    assert result == [
        TrustedPublisher(
            kind="GitHub",
            repository="owner/cherries",
            workflow="release.yaml",
            environment=None,
        ),
    ]


def test_get_trusted_publishers_empty_bundles():
    """Return an empty list when provenance exists but lists no bundles."""
    body = json.dumps({"version": 1, "attestation_bundles": []}).encode()
    with patch(
        "repomatic.pypi.urlopen",
        return_value=_FakeResponse(body),
    ):
        assert (
            get_trusted_publishers(
                "cherries", "1.2.3", "cherries-1.2.3-py3-none-any.whl"
            )
            == []
        )


def test_get_trusted_publishers_network_failure():
    """Return None on URL or network errors."""
    with patch(
        "repomatic.pypi.urlopen",
        side_effect=URLError("not found"),
    ):
        assert (
            get_trusted_publishers(
                "cherries", "1.2.3", "cherries-1.2.3-py3-none-any.whl"
            )
            is None
        )


def test_get_trusted_publishers_invalid_json():
    """Return None when the response body cannot be parsed."""
    with patch(
        "repomatic.pypi.urlopen",
        return_value=_FakeResponse(b"not json"),
    ):
        assert (
            get_trusted_publishers(
                "cherries", "1.2.3", "cherries-1.2.3-py3-none-any.whl"
            )
            is None
        )


def test_get_trusted_publishers_skips_malformed_entries():
    """Skip bundles whose publisher object is missing required fields."""
    payload = {
        "version": 1,
        "attestation_bundles": [
            {"publisher": {"kind": "GitHub"}},
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": "owner/cherries",
                    "workflow": "release.yaml",
                    "environment": "production",
                },
            },
        ],
    }
    body = json.dumps(payload).encode()
    with patch(
        "repomatic.pypi.urlopen",
        return_value=_FakeResponse(body),
    ):
        result = get_trusted_publishers(
            "cherries", "1.2.3", "cherries-1.2.3-py3-none-any.whl"
        )
    assert result == [
        TrustedPublisher(
            kind="GitHub",
            repository="owner/cherries",
            workflow="release.yaml",
            environment="production",
        ),
    ]


def test_settings_url_bare_without_prefill_args():
    """No keyword args returns the bare settings URL."""
    assert pypi_trusted_publisher_settings_url(
        "cherries"
    ) == PYPI_TRUSTED_PUBLISHER_SETTINGS_URL.format(package="cherries")


@pytest.mark.parametrize(
    ("kwargs", "expected_query"),
    [
        ({"owner": "alice"}, "provider=github&owner=alice"),
        (
            {"owner": "alice", "repository": "cherries"},
            "provider=github&owner=alice&repository=cherries",
        ),
        (
            {
                "owner": "alice",
                "repository": "cherries",
                "workflow_filename": "release.yaml",
                "environment": "production",
            },
            "provider=github&owner=alice&repository=cherries"
            "&workflow_filename=release.yaml&environment=production",
        ),
    ],
)
def test_settings_url_prefill_query(
    kwargs: dict[str, str], expected_query: str
) -> None:
    """Provided GitHub fields produce a `?provider=github&…` prefill suffix."""
    url = pypi_trusted_publisher_settings_url("cherries", **kwargs)
    base = PYPI_TRUSTED_PUBLISHER_SETTINGS_URL.format(package="cherries")
    assert url == f"{base}?{expected_query}"


def test_settings_url_prefill_skips_blank_fields():
    """Empty-string fields are dropped from the prefill query."""
    url = pypi_trusted_publisher_settings_url(
        "cherries",
        owner="alice",
        repository="",
        workflow_filename="release.yaml",
        environment=None,
    )
    base = PYPI_TRUSTED_PUBLISHER_SETTINGS_URL.format(package="cherries")
    assert url == f"{base}?provider=github&owner=alice&workflow_filename=release.yaml"


def test_settings_url_url_encodes_unsafe_characters():
    """Special characters in field values are URL-encoded."""
    url = pypi_trusted_publisher_settings_url(
        "cherries", owner="alice & bob", environment="staging/edge"
    )
    assert "owner=alice+%26+bob" in url
    assert "environment=staging%2Fedge" in url
