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

"""Shared JSON-over-HTTP fetch for the API clients.

The single implementation of the GET-and-parse-JSON loop used by the PyPI
({mod}`repomatic.pypi`), npm ({mod}`repomatic.npm`), and GitHub Releases
({mod}`repomatic.github.releases`) clients, so every datasource shares the
same timeout and truncated-body retry semantics. Response caching stays with
the callers: each client owns its cache namespace, TTL, and serialization.
"""

from __future__ import annotations

import json
from http.client import IncompleteRead
from urllib.error import URLError
from urllib.request import Request, urlopen

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


class FetchError(RuntimeError):
    """Raised when a JSON fetch could not complete cleanly.

    Wraps every failure mode of {func}`get_json`: HTTP 4xx/5xx, network
    error, timeout, truncated body (after its one retry), and JSON parse
    error. Callers decide whether a failure is fatal (GitHub pagination,
    where a missing page corrupts the result) or a soft miss (PyPI/npm
    lookups, logged and treated as "no data").
    """


def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = 10,
) -> tuple[Any, bytes]:
    """GET *url* and parse the body as JSON, retrying once on truncation.

    A truncated body (`IncompleteRead`) is transient (a flaky connection or
    an interfering proxy), so it earns one retry; every other failure mode
    fails straight away.

    :param url: The URL to fetch.
    :param headers: Extra request headers, merged over the JSON `Accept`
        default (caller wins on conflict).
    :param timeout: Socket timeout in seconds.
    :return: `(parsed, raw_bytes)`: the decoded JSON value and the raw body
        (for callers that cache the verbatim response).
    :raises FetchError: On any failure (see the class docstring).
    """
    request = Request(url, headers={"Accept": "application/json", **(headers or {})})
    for retry in (True, False):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
            return json.loads(raw), raw
        except (URLError, TimeoutError, json.JSONDecodeError, IncompleteRead) as exc:
            if retry and isinstance(exc, IncompleteRead):
                continue
            raise FetchError(str(exc)) from exc
    raise AssertionError("unreachable")  # pragma: no cover
