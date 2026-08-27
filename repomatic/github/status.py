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

"""Probe [githubstatus.com](https://www.githubstatus.com) on API failures.

When a `gh` or REST call fails with an opaque error, callers can ask this
module whether GitHub is reporting a live incident. The status page is the
source of truth for outages affecting authentication, the REST API, and
Actions, so surfacing it in error messages saves operators from chasing
PAT scopes that aren't actually broken.

The HTTP probe is memoized for the lifetime of the process: a single CLI
invocation that fails ten `gh` calls in a row hits the status endpoint
once. Failures (DNS, timeout, JSON parse) collapse to `None` so the
probe itself never masks the original error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache

from ..http import FetchError, get_json

GITHUB_STATUS_SUMMARY_URL = "https://www.githubstatus.com/api/v2/status.json"
"""Status summary endpoint exposed by Statuspage.

Returns a JSON document with a top-level `status` object containing an
`indicator` (`none`, `minor`, `major`, `critical`,
`maintenance`) and a human-readable `description`.
"""

_PROBE_TIMEOUT_SECONDS = 3.0
"""Hard cap on the status probe.

The probe runs from an error path, so it must not stall the failure
report. Three seconds is enough for a healthy CDN response and short
enough that an unreachable Statuspage doesn't compound the original
problem.
"""

_HEALTHY_INDICATOR = "none"
"""Statuspage indicator that signals no active incident."""


@dataclass(frozen=True)
class GitHubStatus:
    """Snapshot of the [githubstatus.com](https://www.githubstatus.com) summary.

    :param indicator: One of `none`, `minor`, `major`, `critical`,
        `maintenance`.
    :param description: Human-readable summary (like `All Systems Operational`
        or `Partial System Outage`).
    """

    indicator: str
    description: str

    @property
    def is_incident(self) -> bool:
        """Return `True` when Statuspage reports anything other than healthy."""
        return self.indicator != _HEALTHY_INDICATOR

    def annotation(self) -> str:
        """Render a one-line annotation suitable for appending to an error.

        Returns an empty string when no incident is active, so callers can
        concatenate unconditionally.
        """
        if not self.is_incident:
            return ""
        return (
            f"GitHub Status reports an active incident "
            f"({self.indicator}): {self.description}. "
            "See https://www.githubstatus.com for details."
        )


@cache
def get_github_status() -> GitHubStatus | None:
    """Fetch the current [githubstatus.com](https://www.githubstatus.com) summary.

    Memoized for the lifetime of the process: only the first call hits the
    network. Returns `None` when the probe cannot complete cleanly
    (network error, timeout, malformed JSON, missing fields), so callers
    can treat the probe as best-effort and never let it mask the
    underlying error they were trying to annotate.
    """
    try:
        payload, _raw = get_json(
            GITHUB_STATUS_SUMMARY_URL, timeout=_PROBE_TIMEOUT_SECONDS
        )
    # OSError on top of FetchError: the probe runs from an error path, so
    # even an exotic local failure must never mask the error being annotated.
    except (FetchError, OSError) as exc:
        logging.debug(f"GitHub Status probe failed: {exc}")
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        return None
    indicator = status.get("indicator")
    description = status.get("description")
    if not isinstance(indicator, str) or not isinstance(description, str):
        return None
    return GitHubStatus(indicator=indicator, description=description)


def status_annotation() -> str:
    """Return a one-line incident annotation, or empty string when healthy.

    Convenience wrapper around {func}`get_github_status` for the common
    case where callers want to append a string to an error message
    without branching on `None`.
    """
    status = get_github_status()
    if status is None:
        return ""
    return status.annotation()


def with_status_annotation(msg: str, *, sep: str = " ") -> str:
    """Append the incident annotation to *msg* when one is active.

    Returns *msg* unchanged when GitHub reports healthy or the probe fails, so
    callers can wrap any diagnostic without branching. The separator is
    load-bearing: the PAT probes annotate inline, while the `gh` wrapper puts
    the annotation on its own line under a multi-line stderr.
    """
    annotation = status_annotation()
    if annotation:
        return f"{msg}{sep}{annotation}"
    return msg
