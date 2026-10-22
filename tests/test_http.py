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

"""Tests for the shared HTTP fetch and cache mechanics."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from contextlib import contextmanager
from email.message import Message
from http.client import IncompleteRead, RemoteDisconnected
from io import BytesIO
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

import pytest

from repomatic import http
from repomatic.http import (
    ERROR_BODY_EXCERPT,
    USER_AGENT,
    FetchError,
    download,
    error_excerpt,
    get_cached_json,
    get_json,
    get_json_soft,
    get_text,
    request,
)
from tests.conftest import FakeResponse

PAYLOAD = {"time": {"1.0.0": "2026-01-02T00:00:00.000Z"}}
RAW = json.dumps(PAYLOAD).encode()


def _fetch(ttl: int = 3600):
    """Call `get_cached_json` with the fixed npm/papaya coordinates."""
    return get_cached_json(
        "npm", "papaya", "https://example.test/papaya", ttl=ttl, log_label="papaya"
    )


@pytest.mark.parametrize("truncations", [1, 2])
def test_get_json_retries_incomplete_read(truncations):
    """A truncated body is retried after each pause of the backoff."""
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[IncompleteRead(b"")] * truncations + [FakeResponse(RAW)],
        ),
        patch("repomatic.http.time.sleep") as mock_sleep,
    ):
        assert get_json("https://example.test/mango") == PAYLOAD
    assert mock_sleep.call_args_list == [call(1), call(3)][:truncations]


def test_get_json_persistent_incomplete_read():
    """A body truncated on every retry raises FetchError."""
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[IncompleteRead(b"")] * 3,
        ),
        patch("repomatic.http.time.sleep") as mock_sleep,
        pytest.raises(FetchError),
    ):
        get_json("https://example.test/mango")
    assert mock_sleep.call_args_list == [call(1), call(3)]


def test_get_text_decodes_body():
    """A text fetch returns the decoded body rather than a parsed value."""
    with patch("repomatic.http.urlopen", return_value=FakeResponse(b"pomelo\n")):
        assert get_text("https://example.test/fruit") == "pomelo\n"


def test_get_text_rejects_undecodable_body():
    """A body that is not UTF-8 is a failed fetch, not mojibake."""
    with (
        patch("repomatic.http.urlopen", return_value=FakeResponse(b"\xff\xfe")),
        pytest.raises(FetchError),
    ):
        get_text("https://example.test/fruit")


def test_get_text_retries_incomplete_read():
    """A truncated text body earns the same retries as a JSON one."""
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[IncompleteRead(b""), FakeResponse(b"pomelo")],
        ),
        patch("repomatic.http.time.sleep"),
    ):
        assert get_text("https://example.test/fruit") == "pomelo"


def test_get_cached_json_fetches_and_stores():
    """A cache miss fetches over HTTP and stores the raw response body."""
    with (
        patch("repomatic.http.get_cached_response", return_value=None),
        patch("repomatic.http.urlopen", return_value=FakeResponse(RAW)) as mock_open,
        patch("repomatic.http.store_response") as mock_store,
    ):
        result = _fetch()

    assert result == PAYLOAD
    mock_open.assert_called_once()
    mock_store.assert_called_once_with("npm", "papaya", RAW)


def test_get_cached_json_uses_cache_when_fresh():
    """A cache hit is parsed and returned without any HTTP request."""
    with (
        patch("repomatic.http.get_cached_response", return_value=RAW.decode()),
        patch("repomatic.http.urlopen") as mock_open,
    ):
        result = _fetch()

    assert result == PAYLOAD
    mock_open.assert_not_called()


def test_get_cached_json_ignores_corrupt_cache():
    """A cache entry that no longer parses falls through to a fresh fetch."""
    with (
        patch("repomatic.http.get_cached_response", return_value="<<not json>>"),
        patch("repomatic.http.urlopen", return_value=FakeResponse(RAW)) as mock_open,
        patch("repomatic.http.store_response"),
    ):
        result = _fetch()

    assert result == PAYLOAD
    mock_open.assert_called_once()


@pytest.mark.parametrize(
    "patch_kwargs",
    (
        pytest.param(
            {
                "side_effect": HTTPError(
                    "https://example.test", 404, "Not Found", Message(), None
                )
            },
            id="http-error",
        ),
        pytest.param({"return_value": FakeResponse(b"<<not json>>")}, id="not-json"),
    ),
)
def test_get_cached_json_returns_none_on_fetch_error(patch_kwargs):
    """An HTTP, network or parse failure is swallowed into a `None` result."""
    with (
        patch("repomatic.http.get_cached_response", return_value=None),
        patch("repomatic.http.urlopen", **patch_kwargs),
        patch("repomatic.http.store_response") as mock_store,
    ):
        assert _fetch() is None

    # A failed fetch must never persist anything to the cache.
    mock_store.assert_not_called()


def test_get_cached_json_skips_store_when_ttl_disabled():
    """With caching disabled (`ttl == 0`), a fetched body is not stored."""
    with (
        patch("repomatic.http.get_cached_response", return_value=None),
        patch("repomatic.http.urlopen", return_value=FakeResponse(RAW)),
        patch("repomatic.http.store_response") as mock_store,
    ):
        assert _fetch(ttl=0) == PAYLOAD

    mock_store.assert_not_called()


def test_get_json_soft_forwards_headers():
    """A soft read sends the caller's headers over the JSON default."""
    with patch("repomatic.http.urlopen", return_value=FakeResponse(RAW)) as mock_open:
        assert (
            get_json_soft(
                "https://example.test/mango", "mango", headers={"X-Fruit": "1"}
            )
            == PAYLOAD
        )
    sent = mock_open.call_args.args[0]
    assert sent.get_header("X-fruit") == "1"
    assert sent.get_header("Accept") == "application/json"


def test_get_json_soft_misses_quietly():
    """A failure is a `None`, not an exception."""
    assert get_json_soft("https://repomatic.invalid/", "unreachable") is None


def _http_error(code: int, body: bytes) -> HTTPError:
    """An `HTTPError` carrying *body*, as `urlopen` raises it."""
    return HTTPError("https://example.test", code, "Error", Message(), BytesIO(body))


def test_request_returns_status_headers_and_body():
    """A response comes back whole, its headers keyed by lowercase name."""
    response = FakeResponse(
        b"{}", status=201, headers={"X-OAuth-Scopes": "notifications, repo"}
    )
    with patch("repomatic.http.urlopen", return_value=response) as mock_open:
        result = request(
            "https://example.test/basket",
            method="POST",
            data=b'{"fruit": "fig"}',
            headers={"Content-Type": "application/json"},
        )
    assert result.status == 201
    assert result.headers == {"x-oauth-scopes": "notifications, repo"}
    assert result.body == b"{}"
    sent = mock_open.call_args.args[0]
    assert sent.get_method() == "POST"
    assert sent.data == b'{"fruit": "fig"}'
    assert sent.get_header("User-agent") == USER_AGENT
    assert sent.get_header("Content-type") == "application/json"


def test_request_keeps_the_status_and_body_of_an_http_error():
    """An error status reaches the caller with the start of its body."""
    body = b'{"message": "Bad credentials"}' + b" " * (ERROR_BODY_EXCERPT * 2)
    with (
        patch("repomatic.http.urlopen", side_effect=_http_error(401, body)),
        pytest.raises(FetchError) as excinfo,
    ):
        request("https://example.test/basket")
    assert excinfo.value.status == 401
    assert excinfo.value.body.startswith('{"message": "Bad credentials"}')
    assert len(excinfo.value.body) == ERROR_BODY_EXCERPT


class CutShortBody(BytesIO):
    """The body of an error answer whose connection drops mid-read."""

    def read(self, *args: object) -> bytes:
        raise IncompleteRead(b"<Error>")


def test_error_excerpt_reads_an_unreadable_body_as_empty():
    """A body the connection cuts short leaves the refusal without its detail."""
    error = HTTPError("https://example.test", 503, "Error", Message(), CutShortBody())
    assert error_excerpt(error) == ""


@pytest.mark.parametrize(
    "failure",
    (
        pytest.param(RemoteDisconnected("Remote end closed connection"), id="dropped"),
        pytest.param(ConnectionResetError("reset by peer"), id="reset"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
    ),
)
def test_request_wraps_every_transport_failure(failure):
    """A dropped, reset or stalled connection is a `FetchError` with no status."""
    with (
        patch("repomatic.http.urlopen", side_effect=failure),
        pytest.raises(FetchError) as excinfo,
    ):
        request("https://example.test/basket")
    assert excinfo.value.status is None


def test_request_wraps_a_malformed_url():
    """A URL urllib cannot open fails like any other fetch."""
    with pytest.raises(FetchError, match="unknown url type"):
        request("orchard/basket.json")


@pytest.mark.parametrize(
    ("method", "attempts"),
    (
        pytest.param("GET", 3, id="get"),
        pytest.param("HEAD", 3, id="head"),
        pytest.param("POST", 1, id="post"),
        pytest.param("PATCH", 1, id="patch"),
        pytest.param("PUT", 1, id="put"),
    ),
)
def test_request_retries_a_truncated_body_only_when_safe(method, attempts):
    """Only a request safe to repeat is sent again after a truncated body."""
    with (
        patch(
            "repomatic.http.urlopen", side_effect=[IncompleteRead(b"")] * 3
        ) as mock_open,
        patch("repomatic.http.time.sleep"),
        pytest.raises(FetchError),
    ):
        request("https://example.test/basket", method=method)
    assert mock_open.call_count == attempts


class _StreamedBody(FakeResponse):
    """A response whose body comes in fixed chunks, with a `Content-Length`."""

    def __init__(self, data: bytes, *, advertised: int | None = None) -> None:
        length = len(data) if advertised is None else advertised
        super().__init__(data, headers={"Content-Length": str(length)})

    def read(self, size: int = -1) -> bytes:
        return self._data.read(size)


def test_download_streams_the_body_and_returns_its_digest(tmp_path):
    """The body lands in the file, and its SHA-256 comes back."""
    body = b"pomelo " * 20_000
    dest = tmp_path / "pomelo.bin"
    seen: list[int] = []

    @contextmanager
    def progress(total):
        seen.append(total)
        yield seen.append

    with patch("repomatic.http.urlopen", return_value=_StreamedBody(body)):
        digest = download("https://example.test/pomelo", dest, progress=progress)

    assert dest.read_bytes() == body
    assert digest == hashlib.sha256(body).hexdigest()
    # The advertised length comes first, then the size of each chunk.
    assert seen[0] == len(body)
    assert sum(seen[1:]) == len(body)
    assert len(seen) > 2


@pytest.mark.parametrize(
    ("failures", "attempts", "succeeds"),
    (
        pytest.param([URLError("reset")], 2, True, id="network-retried"),
        pytest.param([_http_error(503, b"")], 2, True, id="server-error-retried"),
        pytest.param([_http_error(404, b"")], 1, False, id="client-error-final"),
        pytest.param([URLError("reset")] * 3, 3, False, id="gives-up"),
    ),
)
def test_download_retries_only_what_can_change(tmp_path, failures, attempts, succeeds):
    """A network fault or a server error is tried again; a client error is not."""
    body = b"quince"
    dest = tmp_path / "quince.bin"
    responses = [*failures, _StreamedBody(body)]

    with (
        patch("repomatic.http.urlopen", side_effect=responses) as mock_open,
        patch("repomatic.http.time.sleep"),
    ):
        if succeeds:
            download("https://example.test/quince", dest)
        else:
            with pytest.raises(FetchError):
                download("https://example.test/quince", dest)

    assert mock_open.call_count == attempts
    assert dest.exists() is succeeds


def test_download_refuses_a_truncated_body(tmp_path):
    """A body shorter than its `Content-Length` fails, and leaves no file."""
    dest = tmp_path / "fig.bin"
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[_StreamedBody(b"fig", advertised=10) for _ in range(3)],
        ),
        patch("repomatic.http.time.sleep"),
        pytest.raises(FetchError, match="got 3 of 10 bytes") as caught,
    ):
        download("https://example.test/fig", dest)

    assert caught.value.status is None
    assert not dest.exists()


def test_suite_refuses_an_unmocked_fetch():
    """A fetch that no test stubbed fails at once.

    The `_no_real_http` fixture of `tests/conftest.py` holds this. The
    `.invalid` domain never resolves, so a suite that lost the fixture fails
    here on the message instead of reaching a real host.
    """
    with pytest.raises(FetchError, match="not reached for real"):
        get_text("https://repomatic.invalid/")


@pytest.mark.real_http
def test_real_http_marker_lifts_the_refusal():
    """A test marked `real_http` reaches the real `urlopen`.

    The opt-out of the tests that run a registry tool for real: on a cold cache
    their probe downloads the pinned binary through this module. See
    `skip_unless_tool_runs` in `tests/conftest.py`.
    """
    assert http.urlopen is urllib.request.urlopen
