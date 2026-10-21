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
`GH_TOKEN` is absent.  A `Requires authentication` 401 (a GitHub-side
auth incident or a fine-grained PAT scope quirk) is first retried with
the **same** token after a short back-off, catching transient flaps that
clear on their own.  A `Bad credentials` 401 skips that wait: an expired
or revoked PAT never recovers on a retry.  Either then falls back to
`GITHUB_TOKEN` if available and different.  When every retry path is
exhausted, the raised {class}`GhCommandError` is annotated with the current
[githubstatus.com](https://www.githubstatus.com) summary so operators
are not sent chasing PAT scopes during an upstream incident.
```
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from functools import cache
from subprocess import run

from ..tooling.binaries import BinaryInstallError, ensure_binary
from .status import with_status_annotation

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from typing import Any

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
"""The HTTP status `gh api` writes into its error output, like `HTTP 404`."""

_AUTH_FALLBACK_MARKERS = ("Bad credentials", "Requires authentication")
"""Stderr substrings that mean: the primary token's auth context was
rejected. Both surface as a 401 from the GitHub API. `Bad credentials`
covers expired or revoked tokens; `Requires authentication` covers
GitHub-side auth incidents and fine-grained PAT scope mismatches that
GitHub treats as "no auth present" for that resource. In both cases the
ambient `GITHUB_TOKEN` is a meaningful fallback because it is a
different credential issued by Actions itself."""

_TRANSIENT_AUTH_MARKERS = ("Requires authentication",)
"""Subset of {data}`_AUTH_FALLBACK_MARKERS` worth retrying with the *same*
token. `Bad credentials` is excluded: it means the PAT is expired or revoked,
which no wait reverses. A retry on it only delays the cross-token fallback and
buries the cause under retry warnings. Both markers still reach the cross-token
fallback, where a second credential can recover either."""

_TRANSIENT_AUTH_BACKOFF_SECONDS = (1, 3)
"""Sleep durations between bounded same-token retries on a 401. Empty tuple
disables the retry loop. A `Requires authentication` 401 can come back on one
call and clear on the next with the *same* token, so a short retry absorbs it
before the cross-token fallback or the final raise. Two retries (1s + 3s) cover
the common auth-flap window without hiding a real credential problem behind
seconds of idle wait."""

_TRANSIENT_THROTTLE_MARKERS = (
    "No server is currently available to service your request",
)
"""GitHub's secondary rate limit, as answered to a burst of API calls.

Unlike the primary limit, which counts requests per hour, this one fires on
the shape of the traffic: too many calls in too short a window. A
`sample-metrics` forward pass reads a hundred-odd repositories back to back,
and the star reconstruction pages through each one's star history thirty
weeks at a time; either burst can trip it, which then refuses every remaining
call of the run alike. The refusal lifts on its own within the minute, so the
same-token retry spends its schedule sleeping rather than failing the tail of
the run."""

_TRANSIENT_THROTTLE_BACKOFF_SECONDS = (15, 30, 60)
"""Sleep durations between bounded same-token retries on a secondary limit.

Sized to the refusal's own timescale: it clears within the minute, so the
first wait already lands past most of them, and the schedule's whole budget
of 105 seconds stays cheaper than the re-run a failed sampling pass costs.
Unlike the auth schedule this one never feeds the cross-token fallback: a
throttled token is not a wrong one, and swapping credentials against an
abuse-detection refusal would only spread the burst across two identities."""


class GhCommandError(RuntimeError):
    """A `gh` command that still failed after every retry.

    The message is the command's error output, with the
    [githubstatus.com](https://www.githubstatus.com) annotation when an incident
    is active. The attributes keep what a caller branches on, so no caller has
    to search the message for an HTTP status.
    """

    def __init__(
        self, message: str, *, stdout: str = "", stderr: str | None = None
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        """What the command wrote to its standard output before it failed.

        `gh api graphql` writes the partial `data` here when a query fails
        only in part, see {func}`gh_graphql`.
        """
        self.stderr = message if stderr is None else stderr
        """The command's standard error, without the status annotation.

        Defaults to *message*, which is the error output when no annotation
        was added to it.
        """
        match = _HTTP_STATUS_RE.search(self.stderr)
        self.status: int | None = int(match[1]) if match else None
        """The HTTP status of the failed API call.

        `None` when the error output names none: a network error, a `gh`
        usage error.
        """


class GhOutputError(RuntimeError):
    """A `gh` command that ran, but printed something other than the JSON asked for.

    Kept apart from {class}`GhCommandError`: a caller may treat a command that
    could not run as fatal and an unreadable answer as a soft miss.
    """


def _matched_marker(stderr: str, markers: tuple[str, ...]) -> str | None:
    """Return the first *markers* entry present in *stderr*.

    Returns `None` when none match. Centralizes the marker scan while
    keeping each caller explicit about which set it acts on: the
    transient-retry loop and the cross-token fallback deliberately differ,
    so a shared default would hide the distinction.

    :param stderr: The failed command's standard error.
    :param markers: One of the marker families of this module.
    :return: The matched marker, or `None`.
    """
    return next((m for m in markers if m in stderr), None)


@cache
def gh_executable() -> str:
    """Resolve the `gh` binary every call in this package shells out to.

    Prefers the registry-pinned build over whatever `$PATH` offers, which is
    the rule for any tool repomatic shells out to: the registry pin carries a
    version, a checksum and a cooldown, while `$PATH` carries none of the three
    and hands each runner image (and each developer laptop) a different `gh`.

    ```{caution}
    Falls back to bare `gh` on `$PATH` when the registry build cannot be
    obtained. Unlike a formatter, `gh` is on the critical path of jobs that
    have already done real work (a release publish, an issue upsert), so a
    download failure must not strand them: a hosted runner ships a usable
    `gh`, and degrading to it beats failing the job. The fallback is logged,
    never silent.
    ```

    Memoized: the install-and-verify path runs once per process however many
    of the ~60 call sites fire.
    """
    try:
        return str(ensure_binary("gh"))
    except (BinaryInstallError, OSError) as error:
        logging.warning(
            "Could not install the pinned gh, falling back to whatever is on PATH "
            f"(unpinned and unverified): {error}"
        )
        return "gh"


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


def api_headers() -> dict[str, str]:
    """Build GitHub API request headers, authenticated when a token is present.

    The one place a direct HTTP call to the GitHub API gets its headers, so
    every such call agrees on the `Accept` media type and the authentication
    scheme. `Bearer` is the scheme GitHub documents, and it carries both
    classic and fine-grained tokens.

    A token raises the rate limit from 60 to at least 1,000 requests/hour,
    which matters when iterating every tool and action in CI. Resolution
    follows the canonical {func}`resolve_gh_token` order, so a repo carrying
    only `REPOMATIC_PAT` gets authenticated reads here too, not just through
    the `gh` CLI.

    :return: Request headers, with `Authorization` present only when a token
        is set.
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = resolve_gh_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def gh_env(token: str | None = None) -> dict[str, str] | None:
    """Child environment promoting a token to `GH_TOKEN`, or `None` for none.

    The one spelling of the promotion every `gh`-reading subprocess gets
    (`gh` itself, and `labelmaker`, which reads the same variable): the
    canonical {func}`resolve_gh_token` winner is injected as `GH_TOKEN`, a
    value-preserving no-op when `GH_TOKEN` itself won the resolution.

    :param token: The credential to promote; resolved when omitted. An empty
        resolution returns `None`, leaving the child the parent environment.
    """
    if token is None:
        token = resolve_gh_token()
    return {**os.environ, "GH_TOKEN": token} if token else None


def run_gh_command(args: list[str]) -> str:
    """Run a `gh` CLI command and return stdout.

    Token priority: `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`.
    The `gh` CLI does not recognize `REPOMATIC_PAT`, so when set it is
    injected as `GH_TOKEN`.  A `Requires authentication` 401 from the
    primary token is first retried with the **same** token after a short
    bounded back-off (see `_TRANSIENT_AUTH_BACKOFF_SECONDS`), absorbing
    transient GitHub auth flaps that resolve on their own; a `Bad
    credentials` 401 skips straight past it, since a revoked or expired
    token cannot clear on a retry. A secondary rate-limit refusal gets the
    same treatment on its own, longer schedule (see
    `_TRANSIENT_THROTTLE_BACKOFF_SECONDS`), since it lifts on its own
    within the minute. If 401s persist, the command is
    then retried with `GITHUB_TOKEN` if available and different, letting
    CI jobs degrade gracefully to the standard Actions token instead of
    failing outright on a stale PAT.  When every retry path is exhausted,
    the raised {class}`GhCommandError` carries a
    [githubstatus.com](https://www.githubstatus.com) annotation when an
    incident is active.

    :param args: Command arguments to pass to `gh`.
    :return: The stdout output from the command.
    :raises GhCommandError: If the command fails (after retries and fallback,
        if attempted).
    """
    cmd = [gh_executable(), *args]
    logging.debug(f"Running: {' '.join(cmd)}")

    github_token = os.environ.get("GITHUB_TOKEN")
    primary = resolve_gh_token()
    env = gh_env(primary)
    process = run(cmd, capture_output=True, encoding="UTF-8", check=False, env=env)

    # Bounded same-token retry, before the cross-token fallback. Catches
    # transient GitHub failures a re-run with the same credential clears:
    # auth flaps, within seconds, and secondary rate limits, within the
    # minute, each paying its own schedule. Scoped to those two marker
    # families: anything else must not pay the wait.
    auth_delays = iter(_TRANSIENT_AUTH_BACKOFF_SECONDS)
    throttle_delays = iter(_TRANSIENT_THROTTLE_BACKOFF_SECONDS)
    while process.returncode:
        if marker := _matched_marker(process.stderr, _TRANSIENT_AUTH_MARKERS):
            delay = next(auth_delays, None)
            failure = f"returned 401 ({marker})"
        elif marker := _matched_marker(process.stderr, _TRANSIENT_THROTTLE_MARKERS):
            delay = next(throttle_delays, None)
            failure = f"hit a secondary rate limit ({marker})"
        else:
            break
        if delay is None:
            break
        logging.warning(
            f"gh command {failure}, retrying in {delay}s with the same token."
        )
        time.sleep(delay)
        process = run(cmd, capture_output=True, encoding="UTF-8", check=False, env=env)

    if process.returncode:
        stderr = process.stderr
        # On a 401 from the primary token, fall back to GITHUB_TOKEN if
        # available and different.  Both "Bad credentials" (expired PAT)
        # and "Requires authentication" (GitHub auth incident, scope quirk)
        # are recoverable when a second credential is on hand.
        auth_marker = _matched_marker(stderr, _AUTH_FALLBACK_MARKERS)
        if auth_marker and github_token and github_token != primary:
            logging.warning(
                f"Primary token returned 401 ({auth_marker}), retrying with "
                "GITHUB_TOKEN."
            )
            retry = run(
                cmd,
                capture_output=True,
                encoding="UTF-8",
                check=False,
                env=gh_env(github_token),
            )
            if not retry.returncode:
                return retry.stdout
            logging.warning("GITHUB_TOKEN fallback also failed.")

        logging.debug(f"gh command failed: {stderr}")
        raise GhCommandError(
            with_status_annotation(stderr, sep="\n"),
            stdout=process.stdout,
            stderr=stderr,
        )

    return process.stdout


def parse_create_output(output: str, kind: str) -> tuple[int, str]:
    """Read the number and URL of a thread out of `gh {kind} create` output.

    `gh issue create` and `gh pr create` both print the new thread's URL last,
    in the form `https://github.com/owner/repo/{issues,pull}/123`. Read the
    last line rather than the whole output: `gh` prepends advisory lines of
    its own (a deprecation notice, a "Warning: N uncommitted changes" banner),
    and parsing the joined output turns one of those into an error that reads
    as a failed creation when the thread was in fact created.

    :param output: The raw `gh ... create` standard output.
    :param kind: The thread kind for the error message, `issue` or `pr`.
    :return: The `(number, url)` pair of the created thread.
    :raises RuntimeError: When the output carries no parsable thread URL.
    """
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    url = lines[-1].rstrip("/") if lines else ""
    tail = url.rsplit("/", 1)[-1]
    if not tail.isdigit():
        msg = f"Could not read a thread number from `gh {kind} create`: {output!r}"
        raise RuntimeError(msg)
    return int(tail), url


def gh_json(args: Sequence[str], *, soft: bool = False) -> Any | None:
    """Run a `gh` command that prints JSON, and return the parsed value.

    The one entry point for every `gh` call whose output is JSON: an API read
    (`gh api`) as well as a JSON listing (`gh release view --json`).

    :param args: Command arguments to pass to `gh`.
    :param soft: Return `None` when the command fails or prints something that
        is not JSON, and log the cause at debug level. A caller sets it when a
        failed read only means "nothing to report", as a lint probe does.
    :return: The parsed JSON value, or `None` on a failure when *soft* is set.
    :raises GhCommandError: When the command fails and *soft* is not set.
    :raises GhOutputError: When the output is not JSON and *soft* is not set.
    """
    try:
        output = run_gh_command(list(args))
    except RuntimeError as error:
        if not soft:
            raise
        logging.debug(f"gh {' '.join(args)} failed: {error}")
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        msg = f"gh {' '.join(args)} returned unparsable JSON: {error}"
        if not soft:
            raise GhOutputError(msg) from error
        logging.debug(msg)
        return None


def gh_api_pages(endpoint: str, *, key: str = "", soft: bool = False) -> list[Any]:
    """Read every page of a paginated REST endpoint, and return their items.

    `gh api --paginate --slurp` follows the `Link` headers and wraps every page
    in one JSON array, so the pages parse at once rather than line by line.

    :param endpoint: The REST endpoint, query string included. Send `per_page`
        in it to keep the page count low.
    :param key: The field holding the items, for an endpoint that wraps them in
        an object, like `workflow_runs` for the runs endpoints. Empty for an
        endpoint that answers with an array.
    :param soft: Return an empty list when the command fails or prints
        something that is not JSON, as {func}`gh_json` does.
    :return: The items of every page, in API order.
    :raises GhCommandError: When the command fails and *soft* is not set.
    :raises GhOutputError: When the output is not JSON and *soft* is not set.
    """
    pages = gh_json(["api", "--paginate", "--slurp", endpoint], soft=soft)
    items: list[Any] = []
    for page in pages if isinstance(pages, list) else ():
        batch = page.get(key) if key and isinstance(page, dict) else page
        if isinstance(batch, list):
            items.extend(batch)
    return items


def gh_api_json(args: Sequence[str], *, strict: bool = False) -> Any | None:
    """Run a `gh` command expected to emit JSON, and parse it.

    A failed command and an unparsable output both read as `None`, so a caller
    reports one "could not read it" outcome instead of two it cannot act on
    differently. With *strict*, a failed command raises instead, while an
    unparsable output stays a soft miss (`ci-status` reads runs this way).

    A thin layer over {func}`gh_json`, which new code calls directly.

    :param args: Command arguments to pass to `gh`.
    :param strict: Re-raise the {class}`GhCommandError` of a failed command,
        instead of returning `None`.
    :return: The parsed JSON payload, or `None` when the command failed (unless
        *strict*) or its output did not parse.
    """
    if not strict:
        return gh_json(args, soft=True)
    try:
        return gh_json(args)
    except GhOutputError as error:
        logging.debug(str(error))
        return None


def _graphql_args(query: str, variables: Mapping[str, str | int | bool]) -> list[str]:
    """Build the `gh api graphql` arguments for *query* and its *variables*.

    `--field` reads a value that looks like a number, a boolean or `null` as
    that type. So a string variable travels as `--raw-field`: a repository named
    `2048` stays the string a `String!` variable expects. An integer or a
    boolean travels as `--field`, which keeps its GraphQL type. The query is a
    string too, and travels the same way.

    :param query: The GraphQL query string.
    :param variables: Query variables, by name.
    :return: The arguments to pass to {func}`run_gh_command`.
    """
    args = ["api", "graphql", "--raw-field", f"query={query}"]
    for name, value in variables.items():
        flag = "--raw-field" if isinstance(value, str) else "--field"
        args.extend([flag, f"{name}={value}"])
    return args


def gh_graphql(
    query: str, *, partial: bool = False, **variables: str | int | bool
) -> Any:
    """Run a one-shot GraphQL query through `gh`, and return its `data` envelope.

    The one-shot sibling of {func}`iter_graphql_nodes`, for the queries that
    read a handful of fields off a single object rather than walking a
    connection.

    :param query: The GraphQL query string.
    :param partial: Accept a partial answer. GitHub answers a query that fails
        only in part with the `data` it could resolve, beside its `errors`, and
        `gh` then exits non-zero. With *partial*, that `data` is returned when
        the failed command printed one: the fields that failed read as `null`.
    :param variables: Query variables. See `_graphql_args` for how each
        type travels.
    :return: The response's `data` object, unwrapped.
    :raises GhCommandError: When the `gh` invocation fails (see
        {func}`run_gh_command`), unless *partial* is set and the output still
        holds a `data` object.
    :raises GhOutputError: When the output is not JSON.
    """
    try:
        response = gh_json(_graphql_args(query, variables))
    except GhCommandError as error:
        if not partial:
            raise
        try:
            data = json.loads(error.stdout).get("data")
        except (json.JSONDecodeError, AttributeError):
            data = None
        if not isinstance(data, dict):
            raise
        logging.debug(f"GraphQL query failed in part, keeping its data: {error}")
        return data
    if not isinstance(response, dict):
        msg = f"GraphQL answered something other than an object: {response!r}"
        raise GhOutputError(msg)
    return response["data"]


def iter_graphql_nodes(
    query: str,
    connection_path: Sequence[str],
    variables: Mapping[str, str | int | bool] | None = None,
    *,
    page_size_var: str = "",
    page_size: int = 0,
) -> Iterator[dict[str, Any]]:
    """Iterate a GraphQL connection's nodes, following cursor pagination.

    The shared `gh api graphql` pagination loop: run the query, walk the
    response to the connection object, yield each node, then follow
    `pageInfo.hasNextPage`/`endCursor` until the connection is exhausted.
    The query must declare a `$cursor: String` variable and pass it as
    `after: $cursor`, and its connection must select
    `pageInfo { hasNextPage endCursor }`.

    Null nodes (which GitHub's `search` connection can emit) are skipped.

    :param query: The GraphQL query string.
    :param connection_path: Keys from the response's `data` object down to
        the connection (like `("search",)` or `("user",
        "sponsorshipsAsMaintainer")`).
    :param variables: Query variables. See `_graphql_args` for how each
        type travels.
    :param page_size_var: When set, inject the page size into this query
        variable on every request (the query then controls `first:` with
        it). Leave empty for queries with a hard-coded page size.
    :param page_size: Nodes requested per page; only used with
        *page_size_var*.
    :yields: Each node dict, in API order.
    :raises GhCommandError: When a `gh` invocation fails
        (see {func}`run_gh_command`).
    :raises GhOutputError: When a page is not JSON.
    """
    cursor: str | None = None
    while True:
        page_variables: dict[str, str | int | bool] = dict(variables or {})
        if page_size_var:
            page_variables[page_size_var] = page_size
        if cursor:
            page_variables["cursor"] = cursor

        response = gh_json(_graphql_args(query, page_variables))
        connection = (response.get("data") if isinstance(response, dict) else {}) or {}
        for key in connection_path:
            connection = connection.get(key) or {}

        for node in connection.get("nodes", []):
            if node:
                yield node

        page_info = connection.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
