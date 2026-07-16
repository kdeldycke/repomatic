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

"""Generic wrapper for the `gh` CLI.

```{note}

Workflow steps must set `GH_TOKEN` explicitly: `GITHUB_TOKEN` is a
secret expression in GitHub Actions, not an automatic environment variable.
The standard pattern is `GH_TOKEN: ${{ secrets.REPOMATIC_PAT || github.token }}`
for steps that prefer a PAT, or `GH_TOKEN: ${{ github.token }}` otherwise.

As defense-in-depth, {func}`run_gh_command` promotes `REPOMATIC_PAT` to
`GH_TOKEN` when set, and promotes `GITHUB_TOKEN` to `GH_TOKEN` when
`GH_TOKEN` is absent.  On a 401 from the primary token (either `Bad
credentials` from an expired or revoked PAT, or `Requires
authentication` from a GitHub-side auth incident or a fine-grained PAT
scope quirk) it first retries with the **same** token after a short
back-off (catching transient flaps that clear on their own), then with
`GITHUB_TOKEN` if available and different.  When every retry path is
exhausted, the raised `RuntimeError` is annotated with the current
[githubstatus.com](https://www.githubstatus.com) summary so operators
are not sent chasing PAT scopes during an upstream incident.
```
"""

from __future__ import annotations

import logging
import os
import time
from subprocess import run

from .status import status_annotation

_AUTH_FALLBACK_MARKERS = ("Bad credentials", "Requires authentication")
"""Stderr substrings that mean: the primary token's auth context was
rejected. Both surface as a 401 from the GitHub API. `Bad credentials`
covers expired or revoked tokens; `Requires authentication` covers
GitHub-side auth incidents and fine-grained PAT scope mismatches that
GitHub treats as "no auth present" for that resource. In both cases the
ambient `GITHUB_TOKEN` is a meaningful fallback because it is a
different credential issued by Actions itself."""

_TRANSIENT_AUTH_BACKOFF_SECONDS = (1, 3)
"""Sleep durations between bounded same-token retries on a 401. Empty tuple
disables the retry loop. `Requires authentication` 401s sometimes come
back on the first call and clear on the next within the same workflow
run, no token rotation involved: the click-extra `v7.19.0` release saw
`manage_issue_lifecycle` fail with 401 once, then succeed on a manual
workflow re-run with the *same* `REPOMATIC_PAT`. A single-token retry
absorbs that transient before the cross-token fallback or the final
raise. Stays small on purpose: two retries (1s + 3s) cover the common
GitHub auth-flap window without disguising a genuine credential
problem behind seconds of idle wait."""


def _matched_auth_marker(stderr: str) -> str | None:
    """Return the first `_AUTH_FALLBACK_MARKERS` entry present in *stderr*.

    Returns `None` when none match. Centralizes the marker scan so the
    transient-retry loop and the cross-token fallback agree on what
    counts as a 401 worth recovering from.
    """
    return next((m for m in _AUTH_FALLBACK_MARKERS if m in stderr), None)


def resolve_gh_token() -> str:
    """Return the GitHub token from environment variables.

    The canonical lookup order for every GitHub API access in the package:
    `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`. Empty string when no
    variable is set.
    """
    return (
        os.environ.get("REPOMATIC_PAT")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    )


def run_gh_command(args: list[str]) -> str:
    """Run a `gh` CLI command and return stdout.

    Token priority: `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`.
    The `gh` CLI does not recognize `REPOMATIC_PAT`, so when set it is
    injected as `GH_TOKEN`.  On a 401 from the primary token (`Bad
    credentials` or `Requires authentication`) the command is first
    retried with the **same** token after a short bounded back-off (see
    `_TRANSIENT_AUTH_BACKOFF_SECONDS`), absorbing transient GitHub
    auth flaps that resolve on their own.  If 401s persist, the command is
    then retried with `GITHUB_TOKEN` if available and different, letting
    CI jobs degrade gracefully to the standard Actions token instead of
    failing outright on a stale PAT.  When every retry path is exhausted,
    the raised `RuntimeError` carries a
    [githubstatus.com](https://www.githubstatus.com) annotation when an
    incident is active.

    :param args: Command arguments to pass to `gh`.
    :return: The stdout output from the command.
    :raises RuntimeError: If the command fails (after retries and fallback,
        if attempted).
    """
    cmd = ["gh", *args]
    logging.debug(f"Running: {' '.join(cmd)}")

    # Build the env override for the gh subprocess.  REPOMATIC_PAT takes
    # priority (the canonical {func}`resolve_gh_token` order); otherwise
    # promote GITHUB_TOKEN to GH_TOKEN so the gh CLI finds a token in GitHub
    # Actions (where GH_TOKEN is not set by default). No override when
    # GH_TOKEN is already set natively.
    pat = os.environ.get("REPOMATIC_PAT")
    gh_token = os.environ.get("GH_TOKEN")
    github_token = os.environ.get("GITHUB_TOKEN")
    if pat:
        env = {**os.environ, "GH_TOKEN": pat}
    elif not gh_token and github_token:
        env = {**os.environ, "GH_TOKEN": github_token}
    else:
        env = None
    process = run(cmd, capture_output=True, encoding="UTF-8", check=False, env=env)

    # Bounded same-token retry on a 401 marker, before the cross-token
    # fallback. Catches transient GitHub auth flaps where a re-run with the
    # same credential clears the failure.
    for delay in _TRANSIENT_AUTH_BACKOFF_SECONDS:
        if not process.returncode:
            break
        marker = _matched_auth_marker(process.stderr)
        if marker is None:
            break
        logging.warning(
            "gh command returned 401 (%s), retrying in %ds with the same token.",
            marker,
            delay,
        )
        time.sleep(delay)
        process = run(cmd, capture_output=True, encoding="UTF-8", check=False, env=env)

    if process.returncode:
        stderr = process.stderr
        # On a 401 from the primary token, fall back to GITHUB_TOKEN if
        # available and different.  Both "Bad credentials" (expired PAT)
        # and "Requires authentication" (GitHub auth incident, scope quirk)
        # are recoverable when a second credential is on hand.
        primary = pat or gh_token
        auth_marker = _matched_auth_marker(stderr)
        if auth_marker and github_token and github_token != primary:
            logging.warning(
                "Primary token returned 401 (%s), retrying with GITHUB_TOKEN.",
                auth_marker,
            )
            retry = run(
                cmd,
                capture_output=True,
                encoding="UTF-8",
                check=False,
                env={**os.environ, "GH_TOKEN": github_token},
            )
            if not retry.returncode:
                return retry.stdout
            logging.warning("GITHUB_TOKEN fallback also failed.")

        logging.debug(f"gh command failed: {stderr}")
        annotation = status_annotation()
        if annotation:
            raise RuntimeError(f"{stderr}\n{annotation}")
        raise RuntimeError(stderr)

    return process.stdout
