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

"""Shared HTTP fetch for the API clients.

The single implementation of the GET loop used by the PyPI
({mod}`repomatic.pypi`), npm ({mod}`repomatic.npm`), and GitHub Releases
({mod}`repomatic.github.releases`) clients, so every datasource shares the
same timeout, `User-Agent` and truncated-body retry semantics. Caching
*policy* stays with the callers — each client owns its cache namespace, TTL,
and serialization — while {func}`get_cached_json` shares the raw-response
caching mechanics for the clients that store verbatim bodies.

Most responses are JSON ({func}`get_json`); {func}`get_text` serves the
datasources that are text rather than an API payload (the `astral-sh/setup-uv`
checksum table {mod}`repomatic.release.version_sync` reads, the gitignore.io template
{mod}`repomatic.gitignore` fetches), and {func}`get_bytes` the ones written
back verbatim (a downloaded label-definition file).
"""

from __future__ import annotations

import json
import logging
from http.client import IncompleteRead
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__
from .cache import get_cached_response, store_response

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
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


class FetchError(RuntimeError):
    """Raised when a JSON fetch could not complete cleanly.

    Wraps every failure mode of {func}`get_json`: HTTP 4xx/5xx, network
    error, timeout, truncated body (after its one retry), and JSON parse
    error. Callers decide whether a failure is fatal (GitHub pagination,
    where a missing page corrupts the result) or a soft miss (PyPI/npm
    lookups, logged and treated as "no data").
    """


def _read_body(
    url: str,
    accept: str,
    headers: Mapping[str, str] | None,
    timeout: float,
) -> bytes:
    """GET *url* and return its raw body, retrying once on truncation.

    A truncated body (`IncompleteRead`) is transient (a flaky connection or
    an interfering proxy), so it earns one retry; every other failure mode
    fails straight away.

    :param url: The URL to fetch.
    :param accept: Default `Accept` media type, overridable by *headers*.
    :param headers: Extra request headers, merged over *accept* (caller wins
        on conflict).
    :param timeout: Socket timeout in seconds.
    :return: The raw response body.
    :raises FetchError: On HTTP error, network error, timeout, or a body
        still truncated after the retry.
    """
    request = Request(
        url,
        headers={"Accept": accept, "User-Agent": USER_AGENT, **(headers or {})},
    )
    for retry in (True, False):
        try:
            with urlopen(request, timeout=timeout) as response:
                body: bytes = response.read()
            return body
        except (URLError, TimeoutError, IncompleteRead) as exc:
            if retry and isinstance(exc, IncompleteRead):
                continue
            raise FetchError(str(exc)) from exc
    raise AssertionError("unreachable")  # pragma: no cover


def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Any, bytes]:
    """GET *url* and parse the body as JSON, retrying once on truncation.

    :param url: The URL to fetch.
    :param headers: Extra request headers, merged over the JSON `Accept`
        default (caller wins on conflict).
    :param timeout: Socket timeout in seconds.
    :return: `(parsed, raw_bytes)`: the decoded JSON value and the raw body
        (for callers that cache the verbatim response).
    :raises FetchError: On any failure (see the class docstring).
    """
    raw = _read_body(url, "application/json", headers, timeout)
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as exc:
        raise FetchError(str(exc)) from exc


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


def get_json_soft(url: str, log_label: str) -> tuple[Any, bytes] | None:
    """GET *url* as JSON, logging any failure as a soft miss.

    :param url: The URL to fetch.
    :param log_label: Human-readable label for the debug log on failure.
    :return: `(parsed, raw_bytes)`, or `None` on any failure (HTTP error,
        network error, timeout, JSON parse error).
    """
    try:
        return get_json(url)
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

    fetched = get_json_soft(url, log_label)
    if fetched is None:
        return None
    result, raw = fetched

    if ttl > 0:
        store_response(namespace, key, raw)
    return result
