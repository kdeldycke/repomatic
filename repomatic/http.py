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

"""Shared HTTP client for the API clients.

{func}`request` is the single implementation of the request loop: every
datasource shares its timeout, its `User-Agent`, its truncated-body retry and
the one {class}`FetchError` every failure becomes. Caching *policy* stays with
the callers (each client owns its cache namespace, TTL and serialization),
while {func}`get_cached_json` shares the caching mechanics for the clients
that store verbatim bodies.

Most responses are JSON ({func}`get_json`); {func}`get_text` serves the
datasources that are text rather than an API payload (the `astral-sh/setup-uv`
checksum table {mod}`repomatic.deps.pins` reads, the gitignore.io template
{mod}`repomatic.gitignore` fetches), and {func}`get_bytes` the ones written
back verbatim (a downloaded label-definition file). {func}`download` streams a
large artifact to disk instead of reading it into memory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
import zlib
from contextlib import nullcontext
from http.client import HTTPException, IncompleteRead
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import __version__
from .cache import get_cached_response, store_response

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager
    from pathlib import Path
    from typing import Any


DEFAULT_TIMEOUT = 10
"""Socket timeout in seconds for every HTTP fetch repomatic makes.

A stalled connection must fail the operation, not hang it.
"""

USER_AGENT = f"repomatic/{__version__}"
"""`User-Agent` header sent with every fetch this module makes.

Identifying the client beats urllib's anonymous default on every count: some
registries throttle unidentified agents harder, and an operator reading an
upstream access log can tell which release of this tool hit them.
"""

ACCEPT_ENCODING = "gzip"
"""`Accept-Encoding` header {func}`request` sends, unless the caller overrides it.

A gzip stream carries its own length and checksum in its trailer, so a cut-off
body fails to decode instead of reading as complete. An uncompressed body has no
such check: a CDN can cache a cut-off copy together with a `Content-Length` that
matches the cut. urllib then raises no `IncompleteRead`, and only the parser
finds the damage, with no retry.

PyPI's CDN served the JSON metadata of a package this way, while its gzip copy
was whole. A Trusted Publisher check then read the package as never released.
"""

ERROR_BODY_EXCERPT = 400
"""Characters of an error response body a {class}`FetchError` keeps.

Enough for the error object an API answers with, short of a whole HTML error
page dumped into a log line.
"""

RETRYABLE_METHODS = frozenset(("GET", "HEAD"))
"""Methods whose request is sent again after a truncated response body.

Both are safe to repeat. A truncated answer to a `POST`, `PATCH` or `PUT` says
nothing about whether the server applied the change, so sending it again could
apply it twice.
"""

_TRUNCATED_BODY_BACKOFF_SECONDS = (1, 3)
"""Sleep durations before each retry of a truncated body.

A retry sent at once tends to meet the same flaky connection or interfering
proxy that cut the first read, so each one waits first. Two retries (1s + 3s)
cost little next to a failed fetch, which callers often turn into a skipped
check.
"""

DOWNLOAD_ATTEMPTS = 3
"""Attempts at one {func}`download` before it gives up.

Runner egress fails in ways that clear on the next connection: a
TLS-intercepting proxy, a dropped connection, a short body. A retry after a
pause keeps such a fault from ending a whole release job.
"""

DOWNLOAD_CHUNK_SIZE = 65536
"""Bytes {func}`download` reads from the response, writes and hashes per step."""

DOWNLOAD_TIMEOUT = 30
"""Socket timeout of a {func}`download`, in seconds.

A stall guard, not a transfer budget: `urlopen` applies it to each blocking
socket operation, so a healthy multi-minute download is unaffected while a dead
connection fails in seconds. Larger than {data}`DEFAULT_TIMEOUT`, which is
sized for small API responses.
"""


class FetchError(RuntimeError):
    """Raised when an HTTP request could not complete cleanly.

    Wraps every failure mode of {func}`request`: HTTP 4xx/5xx, network error,
    timeout, malformed URL, truncated body (after its retries), and the decode
    errors of the `get_*` readers. Callers decide whether a failure is fatal
    (GitHub pagination, where a missing page corrupts the result) or a soft
    miss (PyPI/npm lookups, logged and treated as "no data").
    """

    def __init__(
        self, message: str, *, status: int | None = None, body: str = ""
    ) -> None:
        super().__init__(message)
        self.status = status
        """HTTP status of an error response, or `None` when no response came."""
        self.body = body
        """The start of the error response body, empty when there was none.

        At most {data}`ERROR_BODY_EXCERPT` characters, decoded leniently: an API
        error object fits, a whole error page does not.
        """


class Response(NamedTuple):
    """A completed HTTP response."""

    status: int
    """The HTTP status code."""

    headers: dict[str, str]
    """Response headers, keyed by lowercase name."""

    body: bytes
    """The raw response body."""


def error_excerpt(error: HTTPError) -> str:
    """The start of an error response's body, empty when it cannot be read.

    Reading the body is a network read too, and fails the same ways as the
    request: the refusal still stands without its body. Shared with the clients
    that call `urllib` themselves, like the S3 half of
    {mod}`repomatic.cloudflare.r2`.

    :param error: The refusal `urllib` raised.
    :return: At most {data}`ERROR_BODY_EXCERPT` characters of the body, decoded
        leniently.
    """
    try:
        raw = error.read()
    except (OSError, HTTPException, ValueError):
        return ""
    # `urllib` builds an error with no headers when the server sent none.
    encoding = error.headers.get("Content-Encoding", "") if error.headers else ""
    if encoding.lower() == "gzip":
        # Lenient on purpose: an excerpt of a cut-off stream beats none.
        try:
            raw = zlib.decompressobj(wbits=31).decompress(raw)
        except zlib.error:
            return ""
    return raw.decode("UTF-8", errors="replace")[:ERROR_BODY_EXCERPT]


def _decode_body(body: bytes, encoding: str) -> bytes:
    """Undo the `Content-Encoding` of a response body.

    :param body: The body as it came over the wire.
    :param encoding: The `Content-Encoding` header value, empty when absent.
    :return: The decoded body. An encoding other than gzip is left as is, since
        {data}`ACCEPT_ENCODING` asks for nothing else.
    :raises IncompleteRead: When the gzip stream ends early, so {func}`request`
        retries it like any other truncated body.
    :raises FetchError: When the gzip stream is corrupt.
    """
    if encoding.lower() != "gzip" or not body:
        return body
    try:
        return gzip.decompress(body)
    except EOFError as exc:
        raise IncompleteRead(body) from exc
    except (OSError, zlib.error) as exc:
        msg = f"Corrupt gzip body: {exc}"
        raise FetchError(msg) from exc


def request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Response:
    """Send one HTTP request and return the response.

    A truncated body (`IncompleteRead`) is transient (a flaky connection or an
    interfering proxy), so a `GET` or `HEAD` request is sent again after each
    pause in `_TRUNCATED_BODY_BACKOFF_SECONDS`. Every other failure mode, and
    a truncated answer to any other method (see {data}`RETRYABLE_METHODS`),
    fails straight away.

    The request asks for a gzip body ({data}`ACCEPT_ENCODING`), and the
    response body comes back decoded. A gzip stream that ends early counts as a
    truncated body. The headers stay as the server sent them.

    :param url: The URL to request.
    :param method: The HTTP method.
    :param data: The request body, already encoded. A JSON body also needs a
        `Content-Type` header in *headers*.
    :param headers: Request headers, merged over the `User-Agent` default
        (caller wins on conflict).
    :param timeout: Socket timeout in seconds. *data* goes out in one
        `sendall` call, which spends the timeout on the whole body: a large
        upload needs a longer one.
    :return: The status, headers and body of the response.
    :raises FetchError: On an HTTP error status, carrying the status and the
        start of the error body. Also on a network error, a timeout, a URL
        urllib cannot open, or a body still truncated after the last retry.
    """
    retries = (
        _TRUNCATED_BODY_BACKOFF_SECONDS if method.upper() in RETRYABLE_METHODS else ()
    )
    for delay in (*retries, None):
        try:
            prepared = Request(
                url,
                data=data,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Encoding": ACCEPT_ENCODING,
                    **(headers or {}),
                },
                method=method,
            )
            with urlopen(prepared, timeout=timeout) as response:
                received = {
                    key.lower(): value for key, value in response.headers.items()
                }
                body = _decode_body(
                    response.read(), received.get("content-encoding", "")
                )
                return Response(response.status, received, body)
        except IncompleteRead as exc:
            if delay is None:
                raise FetchError(str(exc)) from exc
            logging.debug(f"Truncated body from {url}, retrying in {delay}s: {exc}")
            time.sleep(delay)
        except HTTPError as exc:
            raise FetchError(
                str(exc), status=exc.code, body=error_excerpt(exc)
            ) from exc
        # `OSError` covers `URLError`, a timeout and a reset connection.
        # `HTTPException` covers a server that drops the connection before it
        # answers (`RemoteDisconnected`), and `ValueError` a URL urllib cannot
        # open at all.
        except (OSError, HTTPException, ValueError) as exc:
            raise FetchError(str(exc)) from exc
    raise AssertionError("unreachable")  # pragma: no cover


def _no_progress(size: int) -> None:
    """Ignore the size of a downloaded chunk."""


def _download_once(
    url: str,
    dest: Path,
    timeout: float,
    progress: Callable[[int], AbstractContextManager[Callable[[int], object]]] | None,
) -> str:
    """Stream *url* into *dest* once, with no retry.

    :return: Lowercase hex SHA-256 digest of the body.
    :raises FetchError: On any failure, including a body shorter than its
        `Content-Length`.
    """
    sha256 = hashlib.sha256()
    total = received = 0
    try:
        with (
            urlopen(
                Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout
            ) as response,
            dest.open("wb") as file,
        ):
            total = int(response.headers.get("Content-Length") or 0)
            feedback = (
                progress(total) if progress is not None else nullcontext(_no_progress)
            )
            with feedback as advance:
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                    file.write(chunk)
                    sha256.update(chunk)
                    received += len(chunk)
                    advance(len(chunk))
    except HTTPError as exc:
        raise FetchError(str(exc), status=exc.code, body=error_excerpt(exc)) from exc
    # The same failure modes as `request`, plus a malformed `Content-Length`.
    except (OSError, HTTPException, ValueError) as exc:
        raise FetchError(str(exc)) from exc
    if total and received != total:
        msg = f"Truncated download for {url}: got {received} of {total} bytes."
        raise FetchError(msg)
    return sha256.hexdigest()


def download(
    url: str,
    dest: Path,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    progress: Callable[[int], AbstractContextManager[Callable[[int], object]]]
    | None = None,
) -> str:
    """Stream *url* into *dest* and return the SHA-256 of the body.

    The body goes to disk chunk by chunk, so a large binary never sits in
    memory. A body shorter than its `Content-Length` fails as truncated. Its
    digest would otherwise read as a checksum mismatch, which blames the pin or
    the artifact for a network fault.

    A network failure, a truncated body and a server error (`5xx`) are tried
    again, up to {data}`DOWNLOAD_ATTEMPTS` times, after a pause that grows with
    each attempt. A client error (`4xx`) fails at once: the same request gets
    the same answer. A failed attempt leaves no file behind.

    :param url: The URL to download.
    :param dest: Where to write the body.
    :param timeout: Socket timeout in seconds.
    :param progress: Feedback for an interactive terminal. Called with the
        advertised length (`0` when the server sends none) before the first
        chunk, it returns a context manager. The context manager yields the
        callable that receives the size of each chunk.
    :return: Lowercase hex SHA-256 digest of the body.
    :raises FetchError: On a client error, or when the last attempt fails.
    """
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _download_once(url, dest, timeout, progress)
        except FetchError as exc:
            dest.unlink(missing_ok=True)
            client_error = exc.status is not None and 400 <= exc.status < 500
            if client_error or attempt == DOWNLOAD_ATTEMPTS:
                raise
            logging.warning(
                f"Download attempt {attempt}/{DOWNLOAD_ATTEMPTS} of {url} failed"
                f" ({exc}); retrying."
            )
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _read_body(
    url: str,
    accept: str,
    headers: Mapping[str, str] | None,
    timeout: float,
) -> bytes:
    """GET *url* and return its raw body.

    :param url: The URL to fetch.
    :param accept: Default `Accept` media type, overridable by *headers*.
    :param headers: Extra request headers, merged over *accept* (caller wins
        on conflict).
    :param timeout: Socket timeout in seconds.
    :return: The raw response body.
    :raises FetchError: On any failure of {func}`request`.
    """
    return request(
        url, headers={"Accept": accept, **(headers or {})}, timeout=timeout
    ).body


def _parse_json(raw: bytes) -> Any:
    """Decode a JSON body.

    :raises FetchError: When *raw* is not JSON.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(str(exc)) from exc


def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """GET *url* and parse the body as JSON, retrying on truncation.

    :param url: The URL to fetch.
    :param headers: Extra request headers, merged over the JSON `Accept`
        default (caller wins on conflict).
    :param timeout: Socket timeout in seconds.
    :return: The decoded JSON value.
    :raises FetchError: On any failure (see the class docstring).
    """
    return _parse_json(_read_body(url, "application/json", headers, timeout))


def get_text(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """GET *url* and decode the body as UTF-8 text.

    An undecodable body is a failed fetch rather than something to paper over
    with replacement characters: every caller parses what it reads, and
    parsing mojibake yields a wrong answer instead of a missing one.

    :param url: The URL to fetch.
    :param headers: Extra request headers, merged over the plain-text `Accept`
        default (caller wins on conflict).
    :param timeout: Socket timeout in seconds.
    :return: The decoded response body.
    :raises FetchError: On any failure (see the class docstring), including a
        body that is not valid UTF-8.
    """
    raw = _read_body(url, "text/plain", headers, timeout)
    try:
        return raw.decode("UTF-8")
    except UnicodeDecodeError as exc:
        raise FetchError(str(exc)) from exc


def get_bytes(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """GET *url* and return its raw body.

    For payloads written back verbatim (a downloaded label-definition file),
    where decoding would only risk corrupting bytes nothing here reads.

    :param url: The URL to fetch.
    :param headers: Extra request headers, merged over the wildcard `Accept`
        default (caller wins on conflict).
    :param timeout: Socket timeout in seconds.
    :return: The raw response body.
    :raises FetchError: On any failure (see the class docstring).
    """
    return _read_body(url, "*/*", headers, timeout)


def get_json_soft(
    url: str, log_label: str, *, headers: Mapping[str, str] | None = None
) -> Any | None:
    """GET *url* as JSON, logging any failure as a soft miss.

    :param url: The URL to fetch.
    :param log_label: Human-readable label for the debug log on failure.
    :param headers: Extra request headers, merged over the JSON `Accept`
        default (caller wins on conflict).
    :return: The decoded JSON value, or `None` on any failure (HTTP error,
        network error, timeout, JSON parse error).
    """
    try:
        return get_json(url, headers=headers)
    except FetchError as exc:
        logging.debug(f"{log_label}: {exc}")
        return None


def get_cached_json(
    namespace: str,
    key: str,
    url: str,
    *,
    ttl: int,
    log_label: str,
    force_refresh: bool = False,
) -> Any | None:
    """GET *url* as JSON through the raw-response cache.

    A fresh cached body under `namespace`/`key` short-circuits the network;
    otherwise the response is fetched, cached verbatim (when *ttl* is
    positive), and returned parsed. The caller keeps the caching policy: it
    picks the namespace, the cache key, and the TTL.

    ```{note}
    *force_refresh* skips the cache **read** but keeps the write, which is
    what separates it from `ttl=0`: the latter also skips the store, so a
    caller using it to bypass a stale entry would leave that entry in place
    for the next reader. A forced refresh replaces it.
    ```

    :param namespace: Cache namespace (like `"pypi"` or `"npm"`).
    :param key: Cache key within the namespace, usually the package name.
    :param url: The URL to fetch on a cache miss.
    :param ttl: Freshness TTL in seconds; `0` disables caching.
    :param log_label: Human-readable label for the debug log on failure.
    :param force_refresh: Ignore any cached body and re-fetch, then store
        the fresh response.
    :return: The parsed JSON value, or `None` on any fetch failure.
    """
    cached = None if force_refresh else get_cached_response(namespace, key, ttl)
    if cached is not None:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    try:
        raw = _read_body(url, "application/json", None, DEFAULT_TIMEOUT)
        result = _parse_json(raw)
    except FetchError as exc:
        logging.debug(f"{log_label}: {exc}")
        return None

    if ttl > 0:
        store_response(namespace, key, raw)
    return result
