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

"""Tests for the GitHub Releases API client."""

from __future__ import annotations

import json
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import URLError

import pytest

from repomatic.github.releases import (
    GitHubRelease,
    GitHubReleasesUnavailable,
    extract_version,
    fetch_github_release_notes,
    get_github_releases,
)
from tests.conftest import FakeResponse


def _release_payload(version: str, date: str = "2026-01-01") -> dict:
    """Build a minimal GitHub release JSON object."""
    return {
        "tag_name": f"v{version}",
        "published_at": f"{date}T00:00:00Z",
        "body": "",
    }


def _bypass_cache(monkeypatch):
    """Force cache misses and silently swallow writes."""
    monkeypatch.setattr(
        "repomatic.github.releases.get_cached_response",
        lambda namespace, key, ttl: None,
    )
    monkeypatch.setattr(
        "repomatic.github.releases.store_response",
        lambda namespace, key, data: None,
    )


def test_get_github_releases_single_page(monkeypatch):
    """A single-page fetch returns all releases as a dict."""
    _bypass_cache(monkeypatch)
    body = json.dumps([
        _release_payload("1.1.0", "2026-02-10"),
        _release_payload("1.0.0", "2025-12-01"),
    ]).encode()

    responses = iter([FakeResponse(body), FakeResponse(b"[]")])
    with patch(
        "repomatic.http.urlopen",
        side_effect=lambda *a, **kw: next(responses),
    ):
        result = get_github_releases("https://github.com/user/repo")

    assert result == {
        "1.1.0": GitHubRelease(date="2026-02-10", body=""),
        "1.0.0": GitHubRelease(date="2025-12-01", body=""),
    }


def test_get_github_releases_multi_page_pagination(monkeypatch):
    """Pagination merges results across pages until an empty page."""
    _bypass_cache(monkeypatch)
    page_1 = json.dumps([_release_payload("2.0.0", "2026-03-01")]).encode()
    page_2 = json.dumps([_release_payload("1.0.0", "2026-01-01")]).encode()
    page_3 = b"[]"

    responses = iter([
        FakeResponse(page_1),
        FakeResponse(page_2),
        FakeResponse(page_3),
    ])
    with patch(
        "repomatic.http.urlopen",
        side_effect=lambda *a, **kw: next(responses),
    ):
        result = get_github_releases("https://github.com/user/repo")

    assert set(result) == {"2.0.0", "1.0.0"}


def test_get_github_releases_empty_repo(monkeypatch):
    """A repo with no releases returns an empty dict (not an exception)."""
    _bypass_cache(monkeypatch)
    with patch(
        "repomatic.http.urlopen",
        return_value=FakeResponse(b"[]"),
    ):
        result = get_github_releases("https://github.com/user/repo")

    assert result == {}


def test_get_github_releases_raises_on_url_error(monkeypatch):
    """A URLError on the first page raises GitHubReleasesUnavailable.

    Distinguishes "we don't know" from "no releases" — the legacy
    behavior returned `{}` for both, which let transient API failures
    silently rewrite the changelog.
    """
    _bypass_cache(monkeypatch)
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=URLError("502 Bad Gateway"),
        ),
        pytest.raises(GitHubReleasesUnavailable) as exc_info,
    ):
        get_github_releases("https://github.com/user/repo")
    assert "user/repo" in str(exc_info.value)


def test_get_github_releases_retries_incomplete_read(monkeypatch):
    """A truncated page body gets one retry before the lookup fails."""
    _bypass_cache(monkeypatch)
    page_1 = json.dumps([_release_payload("1.0.0", "2026-01-01")]).encode()
    with patch(
        "repomatic.http.urlopen",
        side_effect=[
            IncompleteRead(b""),
            FakeResponse(page_1),
            FakeResponse(b"[]"),
        ],
    ):
        result = get_github_releases("https://github.com/user/repo")
    assert set(result) == {"1.0.0"}


def test_get_github_releases_raises_on_persistent_incomplete_read(monkeypatch):
    """A page truncated on the retry too raises GitHubReleasesUnavailable."""
    _bypass_cache(monkeypatch)
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=IncompleteRead(b""),
        ),
        pytest.raises(GitHubReleasesUnavailable) as exc_info,
    ):
        get_github_releases("https://github.com/user/repo")
    assert "user/repo" in str(exc_info.value)


def test_get_github_releases_raises_on_partial_pagination(monkeypatch):
    """A URLError on a later page raises rather than silently truncating.

    This is the failure mode behind kdeldycke/click-extra#1702: page 1
    succeeded, page 2 timed out, the legacy `break` returned the partial
    page-1 result, and the caller treated every missing version as
    "no GitHub release exists."
    """
    _bypass_cache(monkeypatch)
    page_1 = json.dumps([_release_payload("3.0.0", "2026-03-01")]).encode()

    responses = iter([FakeResponse(page_1)])
    fail_after_first = [False]

    def fake_urlopen(*args, **kwargs):
        if fail_after_first[0]:
            raise URLError("timeout on page 2")
        fail_after_first[0] = True
        return next(responses)

    with (
        patch("repomatic.http.urlopen", side_effect=fake_urlopen),
        pytest.raises(GitHubReleasesUnavailable) as exc_info,
    ):
        get_github_releases("https://github.com/user/repo")
    assert "page 2" in str(exc_info.value)


def test_get_github_releases_raises_on_timeout(monkeypatch):
    """A TimeoutError surfaces as GitHubReleasesUnavailable."""
    _bypass_cache(monkeypatch)
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=TimeoutError("read timed out"),
        ),
        pytest.raises(GitHubReleasesUnavailable),
    ):
        get_github_releases("https://github.com/user/repo")


def test_get_github_releases_raises_on_invalid_json(monkeypatch):
    """An unparsable response surfaces as GitHubReleasesUnavailable."""
    _bypass_cache(monkeypatch)
    with (
        patch(
            "repomatic.http.urlopen",
            return_value=FakeResponse(b"not json"),
        ),
        pytest.raises(GitHubReleasesUnavailable),
    ):
        get_github_releases("https://github.com/user/repo")


def test_get_github_releases_malformed_url_returns_empty():
    """A URL with no `owner/repo` segment returns an empty dict.

    This is a caller-level bug, not an API failure: nothing to fetch and
    nothing to gate against, so the legacy "return empty" behavior is
    preserved.
    """
    assert get_github_releases("not-a-url") == {}


# ---------------------------------------------------------------------------
# Tag-version extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "pattern", "expected"),
    [
        ("v1.7.12", None, "1.7.12"),
        ("1.7.12", None, "1.7.12"),
        ("lychee-v0.24.2", r"^lychee-v(?P<version>.+)$", "0.24.2"),
        ("@biomejs/biome@2.5.0", r"^@biomejs/biome@(?P<version>.+)$", "2.5.0"),
        ("nope", r"^lychee-v(?P<version>.+)$", None),
    ],
)
def test_extract_version(tag, pattern, expected):
    assert extract_version(tag, pattern) == expected


# ---------------------------------------------------------------------------
# Range-to-release-notes fetch
# ---------------------------------------------------------------------------


def test_fetch_github_release_notes_filters_range_and_sorts():
    fake = {
        "v1.0.0": GitHubRelease(date="2026-01-01", body="old"),  # == old: excluded.
        "v1.1.0": GitHubRelease(date="2026-02-01", body="notes 1.1"),
        "v1.2.0": GitHubRelease(date="2026-03-01", body="notes 1.2"),
        "v1.3.0": GitHubRelease(date="2026-04-01", body="too new"),  # > new: excluded.
    }
    with patch("repomatic.github.releases.get_release_tags", return_value=fake):
        notes = fetch_github_release_notes([
            ("checkout", "https://github.com/actions/checkout", "1.0.0", "1.2.0", None)
        ])
    repo_url, versions = notes["checkout"]
    assert repo_url == "https://github.com/actions/checkout"
    # Only the half-open range (1.0.0, 1.2.0], oldest first.
    assert [tag for tag, _ in versions] == ["v1.1.0", "v1.2.0"]


def test_fetch_github_release_notes_skips_empty_bodies():
    fake = {"v2.0.0": GitHubRelease(date="2026-02-01", body="")}
    with patch("repomatic.github.releases.get_release_tags", return_value=fake):
        notes = fetch_github_release_notes([
            ("x", "https://github.com/o/x", "1.0.0", "2.0.0", None)
        ])
    assert notes == {}


def test_fetch_github_release_notes_graceful_when_unavailable():
    with patch(
        "repomatic.github.releases.get_release_tags",
        side_effect=GitHubReleasesUnavailable("boom"),
    ):
        notes = fetch_github_release_notes([
            ("x", "https://github.com/o/x", "1.0.0", "2.0.0", None)
        ])
    assert notes == {}


def test_fetch_github_release_notes_honors_tag_pattern():
    fake = {
        "lychee-v0.24.0": GitHubRelease(date="2026-02-01", body="notes"),
        "lychee-v0.25.0": GitHubRelease(date="2026-03-01", body="too new"),
    }
    with patch("repomatic.github.releases.get_release_tags", return_value=fake):
        notes = fetch_github_release_notes([
            (
                "lychee",
                "https://github.com/lycheeverse/lychee",
                "0.23.0",
                "0.24.0",
                r"^lychee-v(?P<version>.+)$",
            )
        ])
    _repo, versions = notes["lychee"]
    assert [tag for tag, _ in versions] == ["lychee-v0.24.0"]
