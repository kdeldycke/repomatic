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

"""npm registry API integration.

The npm counterpart to {mod}`repomatic.pypi`, used by `sync-workflow-pins` to
resolve the npm version literals embedded in workflow YAML (like
`npm install awesome-lint@2.3.0`).
"""

from __future__ import annotations

import json
import logging

from .cache import get_cached_response, store_response
from .config import load_repomatic_config
from .http import FetchError, get_json

NPM_REGISTRY_URL = "https://registry.npmjs.org/{package}"
"""npm registry metadata URL for a package."""


def _fetch_json(package: str) -> dict | None:
    """Fetch the full JSON metadata for an npm package.

    Results are cached under the `npm` namespace. Freshness TTL is read from
    `CacheConfig.npm_ttl`. Returns `None` for every failure mode (HTTP error,
    network error, timeout, JSON parse error).

    :param package: The npm package name.
    :return: Parsed JSON response, or `None` on any failure.
    """
    ttl = load_repomatic_config().cache.npm_ttl
    cached = get_cached_response("npm", package, ttl)
    if cached is not None:
        try:
            return json.loads(cached)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            pass

    url = NPM_REGISTRY_URL.format(package=package)
    try:
        fetched, raw = get_json(url)
    except FetchError as exc:
        logging.debug(f"npm lookup failed for {package}: {exc}")
        return None
    result: dict = fetched

    if ttl > 0:
        store_response("npm", package, raw)
    return result


def get_release_dates(package: str) -> dict[str, str]:
    """Get publication dates for all versions of an npm package.

    :param package: The npm package name (e.g. `awesome-lint`).
    :return: Dict mapping version strings to `YYYY-MM-DD` publication dates.
        Empty if the package is not found or the request fails.
    """
    data = _fetch_json(package)
    if data is None:
        return {}

    # The `time` map is keyed by version, plus two housekeeping keys
    # (`created`, `modified`) that are not versions.
    times = data.get("time", {})
    return {
        version: stamp[:10]
        for version, stamp in times.items()
        if version not in ("created", "modified") and stamp
    }
