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

Backs the `cloudflare-pages` command, in the modes of {class}`PagesMode`.
{mod}`repomatic.cloudflare.api` resolves the credential and the account.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum

from boltons.iterutils import get_path
from click_extra import echo

from . import api
from .api import CloudflareError, CloudflareHTTPError, Status, print_status

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any, Final

MISSING: Final = object()
"""What a lookup in the live project answers when the path names nothing."""

SECRET_KEYS: Final = frozenset({"api_token", "oauth_token", "refresh_token", "secret"})
"""Response keys whose values must never be printed."""


class PagesMode(StrEnum):
    """What one run of `cloudflare-pages` does, named after its flag."""

    APPLY = "apply"
    """Write every managed drifted setting back to its declared value."""

    ATTACH_DOMAIN = "attach-domain"
    """Serve the project at a custom domain, DNS record included."""

    CHECK = "check"
    """Diff the live project against the declared state."""

    CREATE = "create"
    """Create the project when missing, then apply the declared settings."""

    DUMP = "dump"
    """Print the live project state as JSON, secrets redacted."""

    @property
    def writes(self) -> bool:
        """Whether the mode writes the declared settings to the live project."""
        return self in {PagesMode.APPLY, PagesMode.CREATE}


@dataclass(frozen=True)
class Setting:
    """One server-side setting, with enough context to justify its value.

    Where the stock default is quoted from Cloudflare's documentation,
    `verified` is true. Where it is inferred from how the product behaves, it
    is false and the diff labels it as such: an unverified default is a
    reasonable guess, not a fact, and this module should not launder one into
    the other.
    """

    path: tuple[str, ...]
    """Keys leading to the value in the project object."""

    desired: Any
    """The value the repository declares."""

    default: Any
    """What a stock Cloudflare Pages project reports for this key."""

    why: str
    """Why the value matters, printed beside a drift."""

    verified: bool = False
    """Whether {attr}`default` is quoted from Cloudflare's documentation."""

    managed: bool = True
    """Whether `--apply` writes the value, or only reports its drift."""

    def describe(self) -> str:
        """Name the setting, its scope and how sure its stock default is."""
        dotted = ".".join(self.path)
        stock = "documented" if self.verified else "inferred, unverified"
        scope = "managed" if self.managed else "read-only"
        return f"{dotted}  [{scope}, stock default {self.default!r}: {stock}]"


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
        replace(setting, path=("deployment_configs", environment, *setting.path))
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
            managed=False,
        ),
    )
    return (*managed, *observed)


def _ensure_dns_record(domain: str, target: str, token: str) -> bool:
    """Point *domain* at *target* with a proxied `CNAME`, if it has no address.

    Cloudflare flattens a `CNAME` sitting at a zone apex into addresses at
    query time, so the same record shape serves an apex and a subdomain alike.
    Proxying is not decoration: it is what puts the request through the edge
    that holds the certificate Pages issued for this hostname.

    :return: Whether the domain now has an address record.
    """
    zone = api.zone_for(domain, token)
    if zone is None:
        print_status(
            Status.NOTE,
            f"could not read the zone for {domain}, so its DNS is left alone."
            f" Create it by hand: a proxied CNAME named {domain} pointing at"
            f" {target}.",
        )
        return False

    records = api.call(f"/zones/{zone['id']}/dns_records?name={domain}", token)
    addressed = [r for r in records if r["type"] in {"A", "AAAA", "CNAME"}]
    if addressed:
        held = ", ".join(f"{r['type']} -> {r['content']}" for r in addressed)
        print_status(Status.OK, f"{domain} already resolves ({held}), left alone.")
        return True

    try:
        created = api.call(
            f"/zones/{zone['id']}/dns_records",
            token,
            method="POST",
            body={
                "type": "CNAME",
                "name": domain,
                "content": target,
                "proxied": True,
                "ttl": 1,
                "comment": "Cloudflare Pages custom domain.",
            },
        )
    except CloudflareError as error:
        print_status(
            Status.NOTE,
            f"the credential cannot write DNS ({str(error).splitlines()[0]})."
            f" Create it by hand: a proxied CNAME named {domain} pointing at"
            f" {target}.",
        )
        return False
    print_status(
        Status.OK,
        f"created {created['type']} {created['name']} -> {created['content']}",
    )
    return True


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
    return value is MISSING or value is None or value == ""


def _diff(
    project: dict[str, Any], settings: tuple[Setting, ...]
) -> tuple[list[Setting], list[tuple[Setting, Any]]]:
    """Split *settings* into those matching the live *project* and those drifted."""
    matched: list[Setting] = []
    drifted: list[tuple[Setting, Any]] = []
    for setting in settings:
        live = get_path(project, setting.path, default=MISSING)
        # A setting asking for "nothing" is satisfied by every spelling of it,
        # so the one the API happens to use today cannot read as drift.
        if _is_unset(setting.desired) and _is_unset(live):
            live = setting.desired
        if live == setting.desired:
            matched.append(setting)
        else:
            drifted.append((setting, live))
    return matched, drifted


def _patch_body(settings: Iterable[Setting]) -> dict[str, Any]:
    """Nest the declared value of every setting into one `PATCH` body."""
    body: dict[str, Any] = {}
    for setting in settings:
        *parents, leaf = setting.path
        node = body
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = setting.desired
    return body


def _attach_domain(project: str, domain: str, token: str, endpoint: str) -> int:
    """Serve *project* at *domain*, DNS included.

    Two operations that the dashboard performs together and the API does not.
    `POST`ing to the project's `domains` collection registers the hostname,
    and that is all it does: no DNS record appears, nothing says one is
    missing, and the domain then reports `verification: active` (Cloudflare
    can see the zone is yours) beside a `status` and a certificate that stay
    `pending` forever, because nothing resolves to the project yet. Doing both
    here is what makes the API path match what the dashboard would have done.

    Idempotent in both halves: an already-attached domain and an already
    addressed hostname are reported and left alone, so a re-run after fixing
    whichever half failed does no harm.

    :return: `0` when the domain is attached and resolving, `1` when the DNS
        half needs a hand, since a domain that resolves to nothing never
        finishes provisioning.
    """
    collection = f"{endpoint}/domains"
    attached = api.call(collection, token)
    if any(entry["name"] == domain for entry in attached):
        print_status(Status.OK, f"{domain} is already attached to {project}.")
    else:
        api.call(collection, token, method="POST", body={"name": domain})
        print_status(Status.OK, f"attached {domain} to {project}.")

    resolving = _ensure_dns_record(domain, f"{project}.pages.dev", token)

    for entry in api.call(collection, token):
        if entry["name"] != domain:
            continue
        certificate = (entry.get("validation_data") or {}).get("status", "unknown")
        print_status(None, f"status={entry.get('status')} certificate={certificate}")
        if entry.get("status") != "active":
            print_status(
                None,
                "Provisioning takes a few minutes once the hostname resolves."
                " Re-run to see it settle.",
            )
    return 0 if resolving else 1


def _create_if_missing(project: str, token: str, account: str, endpoint: str) -> None:
    """Create the Direct Upload project *project*, unless it exists.

    The API answers `409` on a duplicate name rather than converging, so
    probing first is what makes a re-run a no-op that still reconciles: an
    existing project falls through to the same diff and apply path a fresh
    one takes.

    :raises CloudflareHTTPError: When the probe meets any refusal but `404`.
    """
    try:
        api.call(endpoint, token)
    except CloudflareHTTPError as error:
        if error.status != 404:
            raise
        api.call(
            f"/accounts/{account}/pages/projects",
            token,
            method="POST",
            body={"name": project, "production_branch": "main"},
        )
        print_status(Status.OK, f"created Direct Upload project {project!r}.")
    else:
        print_status(Status.OK, f"project {project!r} already exists; reconciling it.")


def run_cloudflare_pages(
    project: str,
    mode: PagesMode,
    *,
    domain: str = "",
    compatibility_date: str = "",
    placement: str = "",
) -> int:
    """Drive one mode of the Pages reconciliation against project *project*.

    `--create` makes the project when it does not exist yet (Direct Upload,
    `main` as its production branch), then applies the declared settings to
    it like `--apply` does. Read-only drift is reported by every mode, and
    keeps the exit code non-zero.

    :param project: Cloudflare Pages project name to reconcile.
    :param mode: What to do with the project.
    :param domain: The hostname {attr}`PagesMode.ATTACH_DOMAIN` serves the
        project at, creating the DNS record it needs when the credential can.
    :param compatibility_date: Declared Workers runtime date, empty for
        unmanaged.
    :param placement: Declared Smart Placement mode, empty for unmanaged.
    :return: Exit code: `0` clean, `1` drift found (or left, for the
        read-only settings no write can change).
    """
    token = api.api_token()
    account = api.account(token, project)
    endpoint = f"/accounts/{account}/pages/projects/{project}"

    if mode is PagesMode.ATTACH_DOMAIN:
        return _attach_domain(project, domain, token, endpoint)
    if mode is PagesMode.CREATE:
        _create_if_missing(project, token, account, endpoint)

    live = api.call(endpoint, token)
    if mode is PagesMode.DUMP:
        echo(json.dumps(_redact(live), indent=2, sort_keys=True))
        return 0

    settings = desired_settings(
        compatibility_date=compatibility_date, placement=placement
    )
    matched, drifted = _diff(live, settings)
    for setting in matched:
        print_status(Status.OK, f"{setting.describe()} = {setting.desired!r}")
    for setting, value in drifted:
        shown = "<absent>" if value is MISSING else repr(value)
        print_status(Status.DRIFT, setting.describe())
        print_status(None, f"live={shown} want={setting.desired!r}")
        print_status(None, setting.why)

    expiry_warning = api.token_expiry_warning(token, account)
    if expiry_warning:
        print_status(Status.WARN, expiry_warning)

    if not drifted:
        echo("\nNo drift. Cloudflare matches what this repository declares.")
        return 0

    writable = [setting for setting, _value in drifted if setting.managed]
    readonly = len(drifted) - len(writable)
    if readonly:
        echo(
            f"\n{readonly} drifted setting(s) are read-only here and must"
            " be changed in the dashboard."
        )

    if not mode.writes:
        echo(f"\n{len(drifted)} drifted setting(s).")
        return 1
    if not writable:
        echo("\nNothing to apply: every drifted setting is read-only.")
        return 1

    api.call(endpoint, token, method="PATCH", body=_patch_body(writable))
    echo(f"\nApplied {len(writable)} setting(s).")
    return 1 if readonly else 0
