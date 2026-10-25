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
from http.client import IncompleteRead
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.pypi import (
    PYPI_TRUSTED_PUBLISHER_SETTINGS_URL,
    TrustedPublisher,
    get_changelog_url,
    get_latest_release_file,
    get_source_url,
    get_trusted_publishers,
    pypi_release_dates,
    pypi_trusted_publisher_settings_url,
    sdist_name_version,
    wait_for_release,
)
from tests.conftest import FakeResponse

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractContextManager


def _patch_pypi_json(
    payload: Mapping[str, object] | None,
) -> AbstractContextManager[MagicMock]:
    """Patch `_fetch_json` to return `payload`."""
    return patch("repomatic.pypi._fetch_json", return_value=payload)


def test_get_source_url_matches_keys_case_insensitively():
    """A lowercase `homepage` key must not fall through to the value scan.

    PyPI preserves whatever spelling a project wrote. A case-sensitive miss used
    to drop through to the "any github.com value" fallback and return whichever
    URL came first, which for these packages is the bug tracker.
    """
    payload = {
        "info": {
            "project_urls": {
                "Bug Tracker": "https://github.com/papaya/kiwi/issues",
                "homepage": "https://github.com/papaya/kiwi",
            }
        }
    }
    with _patch_pypi_json(payload):
        assert get_source_url("kiwi") == "https://github.com/papaya/kiwi"


def test_get_source_url_reduces_a_sub_path_to_the_repo_root():
    """Even the value-scan fallback yields a slug the releases API accepts."""
    payload = {
        "info": {
            "project_urls": {"Bug Tracker": "https://github.com/papaya/kiwi/issues"}
        }
    }
    with _patch_pypi_json(payload):
        assert get_source_url("kiwi") == "https://github.com/papaya/kiwi"


def test_get_source_url_none_without_a_github_url():
    payload = {"info": {"project_urls": {"Docs": "https://kiwi.example.com"}}}
    with _patch_pypi_json(payload):
        assert get_source_url("kiwi") is None


@pytest.mark.parametrize(
    ("project_urls", "expected"),
    [
        pytest.param(
            {"Changelog": "https://kiwi.example.com/changes/"},
            "https://kiwi.example.com/changes",
            id="trailing-slash",
        ),
        # Each key is looked up in priority order, whatever order PyPI lists them.
        pytest.param(
            {
                "History": "https://kiwi.example.com/history",
                "Release Notes": "https://kiwi.example.com/notes",
            },
            "https://kiwi.example.com/notes",
            id="priority",
        ),
        pytest.param({"Docs": "https://kiwi.example.com"}, None, id="no-changelog"),
        # An unreachable package declares no URL at all.
        pytest.param(None, None, id="unreachable"),
    ],
)
def test_get_changelog_url(project_urls, expected):
    """The first changelog-like key wins, with its trailing slash dropped."""
    payload = None if project_urls is None else {"info": {"project_urls": project_urls}}
    with _patch_pypi_json(payload):
        assert get_changelog_url("kiwi") == expected


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


def test_release_dates_capture_yank_reason():
    """The first non-empty per-file yank reason rides along with the release."""
    payload = {
        "releases": {
            "0.9.0": [
                {
                    "filename": "cherries-0.9.0-py3-none-any.whl",
                    "upload_time": "2025-12-01T00:00:00",
                },
            ],
            "1.0.0": [
                {
                    "filename": "cherries-1.0.0-py3-none-any.whl",
                    "upload_time": "2026-01-01T00:00:00",
                    "yanked": True,
                    "yanked_reason": None,
                },
                {
                    "filename": "cherries-1.0.0.tar.gz",
                    "upload_time": "2026-01-01T00:00:00",
                    "yanked": True,
                    "yanked_reason": "Superseded by a corrected upload.",
                },
            ],
        },
    }
    with _patch_pypi_json(payload):
        releases = pypi_release_dates("cherries")
    assert releases["1.0.0"].yanked is True
    assert releases["1.0.0"].yanked_reason == "Superseded by a corrected upload."
    # A live release records no reason at all.
    assert releases["0.9.0"].yanked is False
    assert releases["0.9.0"].yanked_reason == ""


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
        "repomatic.http.urlopen",
        return_value=FakeResponse(body),
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
        "repomatic.http.urlopen",
        return_value=FakeResponse(body),
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
        "repomatic.http.urlopen",
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
        "repomatic.http.urlopen",
        return_value=FakeResponse(b"not json"),
    ):
        assert (
            get_trusted_publishers(
                "cherries", "1.2.3", "cherries-1.2.3-py3-none-any.whl"
            )
            is None
        )


def test_get_trusted_publishers_retries_incomplete_read():
    """A truncated provenance body is retried before the lookup gives up."""
    body = json.dumps({"version": 1, "attestation_bundles": []}).encode()
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[IncompleteRead(b""), FakeResponse(body)],
        ),
        patch("repomatic.http.time.sleep"),
    ):
        assert (
            get_trusted_publishers(
                "cherries", "1.2.3", "cherries-1.2.3-py3-none-any.whl"
            )
            == []
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
        "repomatic.http.urlopen",
        return_value=FakeResponse(body),
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


def test_get_trusted_publishers_skips_bundles_of_the_wrong_shape():
    """Skip bundles and publishers that are no object; drop a non-string environment."""
    payload = {
        "version": 1,
        "attestation_bundles": [
            "papaya",
            {"publisher": "kiwi"},
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": "owner/cherries",
                    "workflow": "release.yaml",
                    "environment": 7,
                },
            },
        ],
    }
    body = json.dumps(payload).encode()
    with patch(
        "repomatic.http.urlopen",
        return_value=FakeResponse(body),
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
            (
                "provider=github&owner=alice&repository=cherries"
                "&workflow_filename=release.yaml&environment=production"
            ),
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


def _dist_dir(tmp_path, *names: str):
    """A directory holding empty distributions named *names*."""
    for name in names:
        (tmp_path / name).touch()
    return tmp_path


def test_sdist_name_version_splits_on_the_last_hyphen(tmp_path):
    """A normalized name holds no hyphen, so the last one splits it off."""
    dist_dir = _dist_dir(
        tmp_path,
        "meta_package_manager-6.2.0-py3-none-any.whl",
        "meta_package_manager-6.2.0.tar.gz",
    )
    assert sdist_name_version(dist_dir) == ("meta_package_manager", "6.2.0")


@pytest.mark.parametrize(
    ("names", "message"),
    (
        (("papaya-1.0.0-py3-none-any.whl",), "holds no sdist"),
        (("papaya.tar.gz",), "Cannot read a project name"),
    ),
)
def test_sdist_name_version_rejects(tmp_path, names, message):
    with pytest.raises(ValueError, match=message):
        sdist_name_version(_dist_dir(tmp_path, *names))


def test_wait_for_release_polls_until_the_version_answers():
    """Each lookup reads the version endpoint afresh, waiting between two."""
    answers = [None, None, {"info": {"version": "1.0.0"}}]
    with (
        patch("repomatic.pypi.get_json_soft", side_effect=answers) as lookup,
        patch("repomatic.pypi.time.sleep") as sleep,
    ):
        assert wait_for_release("papaya", "1.0.0", attempts=5, delay=15) == 3
    assert lookup.call_args.args[0] == "https://pypi.org/pypi/papaya/1.0.0/json"
    assert [call.args for call in sleep.call_args_list] == [(15,), (15,)]


def test_wait_for_release_gives_up_without_a_last_wait():
    with (
        patch("repomatic.pypi.get_json_soft", return_value=None) as lookup,
        patch("repomatic.pypi.time.sleep") as sleep,
    ):
        assert wait_for_release("papaya", "1.0.0", attempts=3, delay=15) is None
    assert lookup.call_count == 3
    assert sleep.call_count == 2


@pytest.mark.parametrize(
    ("answers", "exit_code", "message"),
    (
        ([None, {"info": {}}], 0, "Visible after 2 attempt(s) (~5s)."),
        ([None, None], 1, "papaya 1.0.0 was not visible on PyPI after 2 lookups."),
    ),
)
def test_wait_pypi_cli(tmp_path, answers, exit_code, message):
    """The command reads the release off the sdist and reports how it went."""
    dist_dir = _dist_dir(tmp_path, "papaya-1.0.0.tar.gz")
    with (
        patch("repomatic.pypi.get_json_soft", side_effect=answers),
        patch("repomatic.pypi.time.sleep"),
    ):
        result = CliRunner().invoke(
            repomatic,
            ["wait-pypi", "--dir", str(dist_dir), "--attempts", "2", "--delay", "5"],
        )
    assert result.exit_code == exit_code, result.output
    assert message in result.output
