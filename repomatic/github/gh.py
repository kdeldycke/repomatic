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
The standard pattern is ``GH_TOKEN: ${{ secrets.REPOMATIC_PAT || github.token }}``
for steps that prefer a PAT, or ``GH_TOKEN: ${{ github.token }}`` otherwise.

As defense-in-depth, {func}`run_gh_command` promotes `REPOMATIC_PAT` to
`GH_TOKEN` when set, and promotes `GITHUB_TOKEN` to `GH_TOKEN` when
`GH_TOKEN` is absent.  On a 401 from the primary token (either ``Bad
credentials`` from an expired or revoked PAT, or ``Requires
authentication`` from a GitHub-side auth incident or a fine-grained PAT
scope quirk) it retries with `GITHUB_TOKEN` if available and different.
When the failure still surfaces, the raised `RuntimeError` is annotated
with the current `githubstatus.com <https://www.githubstatus.com>`_
summary so operators are not sent chasing PAT scopes during an
upstream incident.
```
"""

from __future__ import annotations

import logging
import os
from subprocess import run

from .status import status_annotation

_AUTH_FALLBACK_MARKERS = ("Bad credentials", "Requires authentication")
"""Stderr substrings that mean: the primary token's auth context was
rejected. Both surface as a 401 from the GitHub API. ``Bad credentials``
covers expired or revoked tokens; ``Requires authentication`` covers
GitHub-side auth incidents and fine-grained PAT scope mismatches that
GitHub treats as "no auth present" for that resource. In both cases the
ambient `GITHUB_TOKEN` is a meaningful fallback because it is a
different credential issued by Actions itself."""


def run_gh_command(args: list[str]) -> str:
    """Run a `gh` CLI command and return stdout.

    Token priority: `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`.
    The `gh` CLI does not recognize `REPOMATIC_PAT`, so when set it is
    injected as `GH_TOKEN`.  On a 401 from the primary token (``Bad
    credentials`` or ``Requires authentication``) the command is retried
    with `GITHUB_TOKEN` if available and different, letting CI jobs
    degrade gracefully to the standard Actions token instead of failing
    outright on a stale PAT or a transient GitHub auth incident. When the
    fallback is unavailable or also fails, the raised `RuntimeError`
    carries a `githubstatus.com <https://www.githubstatus.com>`_
    annotation when an incident is active.

    :param args: Command arguments to pass to `gh`.
    :return: The stdout output from the command.
    :raises RuntimeError: If the command fails (after fallback, if attempted).
    """
    cmd = ["gh", *args]
    logging.debug(f"Running: {' '.join(cmd)}")

    # Build the env override for the gh subprocess.  REPOMATIC_PAT takes
    # priority; otherwise promote GITHUB_TOKEN to GH_TOKEN so the gh CLI
    # finds a token in GitHub Actions (where GH_TOKEN is not set by default).
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

    if process.returncode:
        stderr = process.stderr
        # On a 401 from the primary token, fall back to GITHUB_TOKEN if
        # available and different.  Both "Bad credentials" (expired PAT)
        # and "Requires authentication" (GitHub auth incident, scope quirk)
        # are recoverable when a second credential is on hand.
        fallback = os.environ.get("GITHUB_TOKEN")
        primary = pat or os.environ.get("GH_TOKEN")
        is_auth_failure = any(m in stderr for m in _AUTH_FALLBACK_MARKERS)
        if is_auth_failure and fallback and fallback != primary:
            logging.warning(
                "Primary token returned 401 (%s), retrying with GITHUB_TOKEN.",
                next(m for m in _AUTH_FALLBACK_MARKERS if m in stderr),
            )
            retry = run(
                cmd,
                capture_output=True,
                encoding="UTF-8",
                check=False,
                env={**os.environ, "GH_TOKEN": fallback},
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
