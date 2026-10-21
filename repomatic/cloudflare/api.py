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

"""Reach the Cloudflare v4 API: the credential, the account and the transport.

{mod}`repomatic.cloudflare.pages` and {mod}`repomatic.cloudflare.r2` share
this module. It also holds the errors they raise and the status labels of
their reports.

The credential resolves in this order, so the same command works in CI and on
a laptop without a token ever landing on a command line:

1. `CLOUDFLARE_API_TOKEN` from the environment (what CI uses).
2. The OAuth token `wrangler login` stores locally.

The account is never declared: the credential settles it. `GET /accounts`
answers what the token sees, and one scoped to nothing but `Cloudflare Pages:
Edit` still enumerates the account it belongs to, so CI carries the token
alone. When a credential sees several accounts, {func}`account` asks which one
owns the project, and fails rather than guesses when that question has no
single answer. No identifier is ever hardcoded either: repositories using this
are public, and account IDs do not belong in them.

```{caution}
Never gate anything on `GET /user/tokens/verify`: that endpoint is
user-scoped, so an account-owned token (the recommended kind, `cfat_` prefix)
answers `401` there while every project call succeeds. Proving the credential
against the project it is meant to touch is the only verification that means
anything, which is what every mode of both commands does implicitly.
```
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import tomlrt
from click_extra import echo
from extra_platforms import is_macos

from ..http import FetchError, request
from ..humanize import format_countdown, parse_iso_datetime
from ..metadata.core import Metadata

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any, Final

    from ..config import Config

API_ROOT: Final = "https://api.cloudflare.com/client/v4"
"""Cloudflare v4 API root every call is relative to."""

API_TIMEOUT: Final = 30
"""Socket timeout in seconds, wider than repomatic's JSON default: a PATCH
that stalls mid-write is worth waiting out rather than retrying blind."""

EXPIRY_WARNING_DAYS: Final = 30
"""How close a token's expiry gets before `--check` starts warning.

Cloudflare notifies about neither an approaching expiry nor a passed one, so
the monthly Docs run carrying this check is the only calendar the token has.
A month of warnings is enough to rotate without ever reaching the red run.
"""

STATUS_WIDTH: Final = 6
"""Columns the label of a report line takes, so the messages line up."""

WRANGLER_CONFIG_FILE: Final = Path(".wrangler/config/default.toml")
"""Where `wrangler login` stores its OAuth token, under a configuration
directory. See {func}`wrangler_config_paths`."""


class CloudflareError(RuntimeError):
    """Raised when the Cloudflare API refuses or a credential cannot be found."""


class CloudflareHTTPError(CloudflareError):
    """An HTTP-level refusal from the Cloudflare API, carrying its status.

    Lets a caller act on *which* refusal arrived (a `404` meaning "create
    it" is a different instruction than a `403` meaning "stop") without
    parsing the message string it was raised with.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status
        """The HTTP status the API answered with."""


class Status(StrEnum):
    """The label that opens one line of a Cloudflare command's report."""

    DRIFT = "DRIFT"
    """The live state differs from the declared one."""

    NOTE = "note"
    """A step the command could not do, left to do by hand."""

    OK = "ok"
    """A step done, or a state that matches the declared one."""

    WARN = "WARN"
    """Nothing fails yet, but something needs a hand soon."""


def print_status(label: Status | None, message: str) -> None:
    """Print one line of a report, its message past the label column.

    :param label: What the line reports. `None` continues the line above it.
    :param message: The text of the line.
    """
    echo(f"{label or '':<{STATUS_WIDTH}}{message}")


def refusal(label: str, status: int | None, detail: str) -> CloudflareError:
    """Build the error a failed request raises, on the REST or the S3 API.

    :param label: The request, like `GET /accounts`. It names the request
        without its URL, which embeds the account ID on the S3 API.
    :param status: The HTTP status of a refusal. `None` when no answer came.
    :param detail: The start of the answer's body for a refusal, else the
        reason the transport gave.
    :return: A {class}`CloudflareHTTPError` for a refusal, else a
        {class}`CloudflareError`.
    """
    if status is None:
        return CloudflareError(f"{label} failed: {detail}")
    msg = f"{label} failed: HTTP {status}\n{detail}".rstrip()
    return CloudflareHTTPError(msg, status)


def wrangler_config_paths() -> tuple[Path, ...]:
    """Where `wrangler login` may have stored its OAuth token, in probe order.

    Resolved on each call, so the home directory and `XDG_CONFIG_HOME` are
    the ones the process sees now, not the ones it saw at import. wrangler
    keeps its configuration in the XDG configuration directory, which macOS
    puts under `~/Library/Preferences`. That path comes first on macOS, and is
    not probed on any other platform.

    :return: The candidate files, most likely first.
    """
    home = Path.home()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    xdg = Path(config_home) if config_home else home / ".config"
    paths = [xdg / WRANGLER_CONFIG_FILE]
    if is_macos():
        paths.insert(0, home / "Library/Preferences" / WRANGLER_CONFIG_FILE)
    return tuple(paths)


def api_token() -> str:
    """Find the credential every call sends.

    :return: `CLOUDFLARE_API_TOKEN`, else the OAuth token of a local
        `wrangler login`.
    :raises CloudflareError: When neither holds a credential, or when the
        `wrangler login` session expired.
    """
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if token:
        return token
    for config_path in wrangler_config_paths():
        if not config_path.is_file():
            continue
        stored = tomlrt.loads(config_path.read_text(encoding="UTF-8"))
        oauth_token = stored.get("oauth_token")
        if not oauth_token:
            continue
        # wrangler refreshes this token transparently with the `refresh_token`
        # it stores beside it; nothing here does, so a session left overnight
        # hands the API a dead credential. Cloudflare answers that with a bare
        # `403` on whichever call comes first, which reads as a scope problem
        # and sends the reader auditing permissions that were never wrong.
        expiry = parse_iso_datetime(stored.get("expiration_time"))
        if expiry and expiry <= datetime.now(UTC):
            msg = (
                f"The `wrangler login` session in {config_path} expired on "
                f"{expiry:%Y-%m-%d %H:%M} UTC. Run `wrangler login` to refresh "
                "it, or set CLOUDFLARE_API_TOKEN."
            )
            raise CloudflareError(msg)
        return str(oauth_token)
    msg = "No credential. Set CLOUDFLARE_API_TOKEN, or run `wrangler login` locally."
    raise CloudflareError(msg)


def call(
    path: str,
    token: str,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
) -> Any:
    """Send one request to the REST API, and return the `result` it answers.

    The request goes through {func}`repomatic.http.request`. A refusal, a
    timeout and a dropped connection all arrive as one error, which this
    turns into a {class}`CloudflareError` the commands catch.

    :param path: The endpoint, relative to {data}`API_ROOT`.
    :param token: The credential, from {func}`api_token`.
    :param method: The HTTP method.
    :param body: The JSON body of a write. `None` for a read.
    :return: The `result` member of the answer.
    :raises CloudflareHTTPError: When the API refuses the request.
    :raises CloudflareError: When no answer came, or when the answer is not
        JSON or reports errors.
    """
    label = f"{method} {path}"
    try:
        response = request(
            f"{API_ROOT}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=API_TIMEOUT,
        )
    except FetchError as error:
        detail = error.body if error.status else str(error)
        if error.status == 403 and path == "/accounts":
            detail += (
                "\n\nScope is an unlikely cause: a token holding nothing but "
                "Cloudflare Pages: Edit enumerates its own account fine. An "
                "expired credential answers 403 here too, and is worth ruling "
                "out first."
            )
        raise refusal(label, error.status, detail) from error
    try:
        payload = json.loads(response.body)
    except json.JSONDecodeError as error:
        msg = f"{label} answered something that is not JSON: {error}"
        raise CloudflareError(msg) from error
    if not payload.get("success", True):
        msg = f"{label} returned errors: {payload.get('errors')}"
        raise CloudflareError(msg)
    return payload["result"]


def account(token: str, project: str) -> str:
    """Find the account to act on, from the credential alone.

    The token settles the usual case on its own: it enumerates its own
    account through `GET /accounts` even when scoped to nothing but
    `Cloudflare Pages: Edit`. A credential seeing several accounts falls
    back to asking where *project* lives, probing each account's copy of
    it: exactly one owner is an answer, anything else fails rather than
    guesses, since deploying into a guessed account is worse than not
    deploying.

    :param token: The credential, from {func}`api_token`.
    :param project: The Pages project, which settles a credential that sees
        several accounts.
    :return: The account ID.
    :raises CloudflareError: When no account, or more than one, answers.
    """
    accounts = call("/accounts", token)
    if not accounts:
        msg = (
            "This credential sees no account at all, so there is nothing to "
            "deploy into. Check the token was created under the account "
            "owning the Pages project."
        )
        raise CloudflareError(msg)
    if len(accounts) == 1:
        return str(accounts[0]["id"])
    owners = []
    for entry in accounts:
        try:
            call(f"/accounts/{entry['id']}/pages/projects/{project}", token)
        except CloudflareError:
            continue
        owners.append(entry)
    if len(owners) == 1:
        return str(owners[0]["id"])
    visible = ", ".join(entry["name"] for entry in accounts)
    if not owners:
        msg = (
            f"This credential sees {len(accounts)} accounts ({visible}) and "
            f"none of them holds a Pages project named {project!r}. Create "
            "the token under the account you want to deploy into: that is "
            "the only thing left that says where the project belongs."
        )
        raise CloudflareError(msg)
    holding = ", ".join(entry["name"] for entry in owners)
    msg = (
        f"This credential sees several accounts holding a Pages project "
        f"named {project!r} ({holding}), so which one to act on has no "
        "single answer. Create a token under the account you mean."
    )
    raise CloudflareError(msg)


def token_expiry_warning(token: str, account_id: str) -> str | None:
    """Warn when the credential expires soon.

    Best effort by design, and it must never fail a run: the account-scoped
    verify endpoint answers for account-owned tokens, the user-scoped one for
    user tokens, and each rejects the other kind. A credential that verifies
    nowhere still deploys fine, so an unreadable expiry is silence, not an
    error.

    :param token: The credential, from {func}`api_token`.
    :param account_id: The account, from {func}`account`.
    :return: The warning, or `None` when the expiry is far, absent or
        unreadable.
    """
    for path in (f"/accounts/{account_id}/tokens/verify", "/user/tokens/verify"):
        try:
            result = call(path, token)
        except CloudflareError:
            continue
        # A verified token with no expiry recorded never expires, so there is
        # nothing to count down to.
        expiry = parse_iso_datetime((result or {}).get("expires_on"))
        if expiry is None:
            return None
        now = datetime.now(UTC)
        if expiry - now >= timedelta(days=EXPIRY_WARNING_DAYS):
            return None
        # A countdown drops its relative phrase once the deadline passes, so
        # a token already dead needs the other verb to read right.
        verb = "expires" if expiry > now else "expired on"
        return (
            f"the API token {verb} {format_countdown(expiry, now)}, and"
            " Cloudflare will not warn anyone. Rotate now: create the"
            " replacement, update the CLOUDFLARE_API_TOKEN secret, verify with"
            " a real deploy, then revoke the old token."
        )
    return None


def zone_for(domain: str, token: str) -> dict[str, Any] | None:
    """Find the zone hosting *domain*, or `None` if the credential sees none.

    The zone is rarely the domain itself: `docs.example.com` lives in
    `example.com`. Labels are stripped from the left until a zone answers,
    stopping at two, which is as far as a registrable name goes for the
    ordinary suffixes. Reading zones needs `Zone → Read`, which a deploy token
    scoped to Pages alone does not carry, so this answers `None` rather than
    raising: the DNS half of attaching a domain is optional work that a
    narrower credential cannot do.

    :param domain: The hostname to place.
    :param token: The credential, from {func}`api_token`.
    :return: The zone object, or `None` when no zone answers.
    """
    labels = domain.split(".")
    for start in range(len(labels) - 1):
        candidate = ".".join(labels[start:])
        try:
            zones = call(f"/zones?name={candidate}", token)
        except CloudflareError:
            return None
        if zones:
            return dict(zones[0])
    return None


def resolve_project(config: Config, project: str | None = None) -> str:
    """Name the Pages project a command acts on.

    :param config: The `[tool.repomatic]` configuration.
    :param project: The project the caller named, if any.
    :return: *project*, else `site.cloudflare-project`, else the repository
        name. Empty when none of them names a project.
    """
    return project or config.site_cloudflare_project or Metadata().repo_name or ""
