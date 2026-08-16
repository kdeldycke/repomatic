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

"""Reconcile a Cloudflare Pages project against the state its repository declares.

A Direct Upload project is not reproducible from anything committed:
`wrangler.toml` only describes what a *build* would need, and these projects
are never built by Cloudflare. Everything that actually shapes the live site
(the compatibility date, Smart Placement, the build image, whether a git
source got attached) lives server-side in the project's `deployment_configs`
and is invisible to anyone reading the repository. One project's compatibility
date sat three years behind the live value with nothing noticing. This module
makes that state explicit, diffable and re-applicable, from the
`[tool.repomatic] site.*` keys.

Backs the `cloudflare-pages` command, in four modes: `--check` diffs live
against declared and exits non-zero on drift, `--apply` writes the declared
values back, `--create` creates the Pages project then applies, and `--dump`
prints the live state with secrets redacted.

Credentials resolve in this order, so the same command works in CI and on a
laptop without a token ever landing on a command line:

1. `CLOUDFLARE_API_TOKEN` from the environment (what CI uses).
2. The OAuth token `wrangler login` stores locally.

The account comes from `CLOUDFLARE_ACCOUNT_ID` when set, otherwise from
`GET /accounts` when the credential can see exactly one. No identifier is ever
hardcoded: repositories using this are public, and account IDs do not belong
in them.

```{caution}
Never gate anything on `GET /user/tokens/verify`: that endpoint is
user-scoped, so an account-owned token (the recommended kind, `cfat_` prefix)
answers `401` there while every project call succeeds. Proving the credential
against the project it is meant to touch is the only verification that means
anything, which is what every mode here does implicitly.
```
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import tomlrt
from click_extra import echo

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final

API_ROOT = "https://api.cloudflare.com/client/v4"
"""Cloudflare v4 API root every call below is relative to."""

API_TIMEOUT = 30
"""Socket timeout in seconds, wider than repomatic's JSON default: a PATCH
that stalls mid-write is worth waiting out rather than retrying blind."""

EXPIRY_WARNING_DAYS = 30
"""How close a token's expiry gets before `--check` starts warning.

Cloudflare notifies about neither an approaching expiry nor a passed one, so
the monthly Docs run carrying this check is the only calendar the token has.
A month of warnings is enough to rotate without ever reaching the red run.
"""

WRANGLER_CONFIG_PATHS: Final = (
    Path.home() / "Library/Preferences/.wrangler/config/default.toml",
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    / ".wrangler/config/default.toml",
)
"""Where `wrangler login` stores its OAuth token, macOS first then XDG."""

SECRET_KEYS = frozenset({"api_token", "oauth_token", "refresh_token", "secret"})
"""Response keys whose values must never be printed."""


class CloudflareError(RuntimeError):
    """Raised when the Cloudflare API refuses or a credential cannot be found."""


@dataclass(frozen=True)
class Setting:
    """One server-side setting, with enough context to justify its value.

    `default` is what a stock Cloudflare Pages project reports for this key.
    Where that value is quoted from Cloudflare's documentation, `verified` is
    True. Where it is inferred from how the product behaves, it is False and
    the diff labels it as such: an unverified default is a reasonable guess,
    not a fact, and this module should not launder one into the other.
    """

    path: tuple[str, ...]
    desired: Any
    default: Any
    why: str
    verified: bool = False
    managed: bool = True


def desired_settings(
    compatibility_date: str = "",
    placement: str = "",
) -> tuple[Setting, ...]:
    """The settings to enforce, from the repository's `site.*` declarations.

    Only what the repository declares is managed, plus one documented floor:
    the build image major version, which Cloudflare auto-migrates old projects
    onto (v1 on 2026-09-15, v2 on 2027-02-23) and then freezes, so asserting
    `3` requests nothing from a current project and names the stragglers.

    Pages supports exactly `production` and `preview`, so the two environments
    are enumerated rather than globbed, and every managed setting applies
    identically to both.

    :param compatibility_date: `site.cloudflare-compatibility-date`, empty to
        leave the live value unmanaged.
    :param placement: `site.cloudflare-placement`, empty to leave the live
        value unmanaged.
    """
    per_environment: list[Setting] = []
    if compatibility_date:
        per_environment.append(
            Setting(
                path=("compatibility_date",),
                desired=compatibility_date,
                default="<project creation date>",
                why=(
                    "Pins the Workers runtime for Pages Functions. Inert while "
                    "the project has no Functions, which is exactly why it "
                    "drifts unnoticed: pinned rather than left to rot."
                ),
                verified=False,
            )
        )
    if placement:
        per_environment.append(
            Setting(
                path=("placement", "mode"),
                desired=placement,
                default="off",
                why=(
                    "Declared so the dashboard toggle stops looking like an "
                    "accident. For a static site it changes nothing measurable "
                    "and costs nothing."
                ),
                verified=False,
            )
        )
    per_environment.append(
        Setting(
            path=("build_image_major_version",),
            desired=3,
            default=3,
            why=(
                "v1 auto-migrates to v3 on 2026-09-15 and v2 on 2027-02-23, "
                "after which Cloudflare states there will be no further build "
                "image versions. Asserts a floor rather than requesting a "
                "change."
            ),
            verified=True,
        )
    )

    managed = tuple(
        Setting(
            path=("deployment_configs", environment, *setting.path),
            desired=setting.desired,
            default=setting.default,
            why=setting.why,
            verified=setting.verified,
        )
        for environment in ("production", "preview")
        for setting in per_environment
    )

    # Settings that are read and reported but never written, because the API
    # path is not a simple PATCH or because getting them wrong breaks
    # publishing. They still belong in the diff: an unmanaged setting that
    # changes is exactly the kind of drift that goes unnoticed for a year.
    observed = (
        Setting(
            path=("source",),
            desired=None,
            default="a `github` source block",
            why=(
                "Direct Upload only: CI builds the site and uploads it "
                "pre-rendered, and Cloudflare must never build it. Anything "
                "other than null here means a source repository got attached, "
                "which makes Cloudflare a second, competing publisher for the "
                "same project."
            ),
            verified=False,
            managed=False,
        ),
        Setting(
            path=("build_config", "build_command"),
            desired="",
            default="",
            why=(
                "Empty because Cloudflare never builds this project. A build "
                "command appearing here means something is trying to make it "
                "build one."
            ),
            verified=False,
            managed=False,
        ),
    )
    return (*managed, *observed)


def _parse_timestamp(value: object) -> datetime | None:
    """Read one of Cloudflare's or wrangler's timestamps, or `None` if unreadable.

    Both spell UTC with a trailing `Z`, which `datetime.fromisoformat` only
    learned to parse in Python 3.11, below this project's floor.
    """
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if token:
        return token
    for config_path in WRANGLER_CONFIG_PATHS:
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
        expiry = _parse_timestamp(stored.get("expiration_time"))
        if expiry and expiry <= datetime.now(timezone.utc):
            msg = (
                f"The `wrangler login` session in {config_path} expired on "
                f"{expiry:%Y-%m-%d %H:%M} UTC. Run `wrangler login` to refresh "
                "it, or set CLOUDFLARE_API_TOKEN."
            )
            raise CloudflareError(msg)
        return str(oauth_token)
    msg = "No credential. Set CLOUDFLARE_API_TOKEN, or run `wrangler login` locally."
    raise CloudflareError(msg)


def _call(
    path: str, token: str, method: str = "GET", body: dict[str, Any] | None = None
) -> Any:
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        if error.code == 403 and path == "/accounts":
            detail += (
                "\n\nA minimum-scope Pages token cannot enumerate accounts, "
                "and neither can an expired credential of any kind. This call "
                "is only attempted to guess the target when "
                "CLOUDFLARE_ACCOUNT_ID is unset. Set CLOUDFLARE_ACCOUNT_ID "
                "and it goes away."
            )
        msg = f"{method} {path} failed: HTTP {error.code}\n{detail}"
        raise CloudflareError(msg) from error
    except urllib.error.URLError as error:
        msg = f"{method} {path} failed: {error.reason}"
        raise CloudflareError(msg) from error
    if not payload.get("success", True):
        msg = f"{method} {path} returned errors: {payload.get('errors')}"
        raise CloudflareError(msg)
    return payload["result"]


def _account(token: str) -> str:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if account:
        return account
    accounts = _call("/accounts", token)
    if len(accounts) != 1:
        names = ", ".join(entry["name"] for entry in accounts) or "none"
        msg = (
            f"Set CLOUDFLARE_ACCOUNT_ID: the credential sees {len(accounts)} "
            f"accounts ({names}), so the target is ambiguous."
        )
        raise CloudflareError(msg)
    return str(accounts[0]["id"])


def _token_expiry_warning(token: str, account: str) -> str | None:
    """A warning line when the credential expires soon, else `None`.

    Best effort by design, and it must never fail a run: the account-scoped
    verify endpoint answers for account-owned tokens, the user-scoped one for
    user tokens, and each rejects the other kind. A credential that verifies
    nowhere still deploys fine, so an unreadable expiry is silence, not an
    error.
    """
    for path in (f"/accounts/{account}/tokens/verify", "/user/tokens/verify"):
        try:
            result = _call(path, token)
        except CloudflareError:
            continue
        expires_on = (result or {}).get("expires_on")
        if not expires_on:
            # Verified, but no expiry recorded: an eternal token. Nothing to
            # count down to, so nothing to warn about here.
            return None
        expiry = _parse_timestamp(expires_on)
        if expiry is None:
            return None
        remaining = expiry - datetime.now(timezone.utc)
        if remaining.days < EXPIRY_WARNING_DAYS:
            return (
                f"WARN  the API token expires {expires_on} ({remaining.days} "
                "days away), and Cloudflare will not warn anyone. Rotate now: "
                "create the replacement, update the CLOUDFLARE_API_TOKEN "
                "secret, verify with a real deploy, then revoke the old token."
            )
        return None
    return None


def _dig(node: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return KeyError
        node = node[key]
    return node


def _nest(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    *parents, leaf = path
    nested: dict[str, Any] = {leaf: value}
    for key in reversed(parents):
        nested = {key: nested}
    return nested


def _merge(into: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    for key, value in other.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value
    return into


def _redact(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: ("<redacted>" if key in SECRET_KEYS else _redact(value))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_redact(value) for value in node]
    return node


def _is_unset(value: Any) -> bool:
    """Whether *value* is one of the ways Cloudflare spells "nothing here".

    A field with no value reads back as a null, as an empty string, or as a
    key that is simply absent, and which one arrives depends on the field and
    on how old the project is. A project created seconds ago carries no
    `build_config` object at all, which is the strongest possible statement
    that it has no build command: without this equivalence it reads as drift
    against the empty string declaring exactly that, and `--create` closes by
    reporting a fault in the project it just made correctly.
    """
    return value is KeyError or value is None or value == ""


def _diff(
    project: dict[str, Any], settings: tuple[Setting, ...]
) -> tuple[list[Setting], list[tuple[Setting, Any]]]:
    """Split *settings* into those matching the live *project* and those drifted."""
    matched: list[Setting] = []
    drifted: list[tuple[Setting, Any]] = []
    for setting in settings:
        live = _dig(project, setting.path)
        # A setting asking for "nothing" is satisfied by every spelling of it,
        # so the one the API happens to use today cannot read as drift.
        if _is_unset(setting.desired) and _is_unset(live):
            live = setting.desired
        if live == setting.desired:
            matched.append(setting)
        else:
            drifted.append((setting, live))
    return matched, drifted


def _describe(setting: Setting) -> str:
    dotted = ".".join(setting.path)
    stock = "documented" if setting.verified else "inferred, unverified"
    scope = "managed" if setting.managed else "read-only"
    return f"{dotted}  [{scope}, stock default {setting.default!r}: {stock}]"


def run_cloudflare_pages(
    project: str,
    *,
    check: bool = False,
    apply: bool = False,
    dump: bool = False,
    create: bool = False,
    account_id: bool = False,
    compatibility_date: str = "",
    placement: str = "",
) -> int:
    """Drive one mode of the Pages reconciliation against project *project*.

    Exactly one of *check*, *apply*, *dump* or *create* must be set; the CLI
    enforces that before calling.

    :param project: Cloudflare Pages project name to reconcile.
    :param check: Diff live against declared, exit 1 on any drift.
    :param apply: PATCH every managed drifted setting back to its declared
        value. Read-only drift is reported and keeps the exit code non-zero.
    :param dump: Print the live project state as JSON, secrets redacted.
    :param create: Create the Pages project (Direct Upload, `main` as its
        production branch), then apply the declared settings to it.
    :param account_id: Print the resolved account identifier and stop. Emits
        the bare value with nothing around it, so it pipes straight into
        whatever needs to store it, and needs no project to exist yet.
    :param compatibility_date: Declared Workers runtime date, empty for
        unmanaged.
    :param placement: Declared Smart Placement mode, empty for unmanaged.
    :return: Exit code: `0` clean, `1` drift found (or left, for the
        read-only settings `--apply` cannot write).
    """
    token = _token()
    account = _account(token)

    if account_id:
        # Bare, so it pipes into whatever stores it. Everything explanatory
        # would have to be stripped back off by the caller.
        echo(account)
        return 0

    endpoint = f"/accounts/{account}/pages/projects/{project}"

    if create:
        _call(
            f"/accounts/{account}/pages/projects",
            token,
            method="POST",
            body={"name": project, "production_branch": "main"},
        )
        echo(f"Created Direct Upload project {project!r}.")

    live = _call(endpoint, token)

    if dump:
        echo(json.dumps(_redact(live), indent=2, sort_keys=True))
        return 0

    settings = desired_settings(
        compatibility_date=compatibility_date, placement=placement
    )
    matched, drifted = _diff(live, settings)

    for setting in matched:
        echo(f"ok    {_describe(setting)} = {setting.desired!r}")
    for setting, value in drifted:
        shown = "<absent>" if value is KeyError else repr(value)
        echo(
            f"DRIFT {_describe(setting)}\n        live={shown} want={setting.desired!r}"
        )
        echo(f"        {setting.why}")

    expiry_warning = _token_expiry_warning(token, account)
    if expiry_warning:
        echo(expiry_warning)

    if not drifted:
        echo("\nNo drift. Cloudflare matches what this repository declares.")
        return 0

    writable = [(setting, value) for setting, value in drifted if setting.managed]
    readonly = [(setting, value) for setting, value in drifted if not setting.managed]

    if readonly:
        echo(
            f"\n{len(readonly)} drifted setting(s) are read-only here and must"
            " be changed in the dashboard."
        )

    if check:
        echo(f"\n{len(drifted)} drifted setting(s).")
        return 1

    # From here on the run is `--apply`, or `--create` finishing the job: a
    # freshly created project falls through to the same write path, since
    # creating without configuring would leave every declared setting drifted.
    assert apply or create

    if not writable:
        echo("\nNothing to apply: every drifted setting is read-only.")
        return 1

    body: dict[str, Any] = {}
    for setting, _value in writable:
        _merge(body, _nest(setting.path, setting.desired))
    _call(endpoint, token, method="PATCH", body=body)
    echo(f"\nApplied {len(writable)} setting(s).")
    return 1 if readonly else 0
