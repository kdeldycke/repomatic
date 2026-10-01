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

"""Serve the files a static site cannot upload to Cloudflare Pages from R2.

Cloudflare Pages Direct Upload rejects any file over 25 MiB, and `wrangler`
fails the whole deploy on the first one it meets. The `--offload` mode of
`repomatic cloudflare-r2` runs between the build and the upload. It moves each
file over the limit to an R2 bucket, and the built `_redirects` sends the old
path to the copy. The site's sources do not change, so the same step serves a
Sphinx tree, a Pelican blog or a hand-built site.

```{note}
Each design choice below prevents a specific failure:

- **A redirect, not a rewritten link.** A redirect acts on the URL, so it needs
  no knowledge of the markup that produced the link. `_redirects` cannot proxy
  to another host (status `200` only rewrites a relative path). A Pages Function
  could, but it puts a Worker in front of every request of a static site.
- **Keys named by content.** An object lives at ``{sha256}/{name}``. A key that
  exists already holds the same bytes, so a re-run uploads nothing. A preview
  deploy cannot overwrite the object that production serves, and a browser can
  cache the object forever.
- **Status `302`.** The target changes each time the file changes. A browser
  that cached a `301` would keep fetching the old bytes.
- **Generated rules first.** An exact rule is free of the 100-rule dynamic
  budget only above the first dynamic rule. The merged file goes through
  {mod}`repomatic.pages_redirects` before it is written.
- **HTML stays in the tree.** A page served from the bucket's host would
  resolve its relative links against that host.
- **The bucket is never pruned.** Old Pages deployments stay online, and they
  still redirect to the objects they knew.
```

Uploads use the S3 API with a key pair limited to the one bucket
(`Object Read & Write`), read from `CLOUDFLARE_R2_ACCESS_KEY_ID` and
`CLOUDFLARE_R2_SECRET_ACCESS_KEY`. Cloudflare scopes a credential to a bucket on
the S3 API only: its REST API needs `Workers R2 Storage Write`, which reaches
every bucket of the account. The request signer is AWS Signature Version 4 on
the standard library, tested against AWS's published vectors, so no S3 SDK
joins the dependencies. The S3 endpoint embeds the account ID, which comes
from `CLOUDFLARE_API_TOKEN` the same way {mod}`repomatic.cloudflare` derives
it.

`--create` and `--check` manage the bucket itself through the REST API. They
run on a maintainer's machine with the `wrangler login` session: its
`workers:write` scope covers R2 (verified on 2026-09-30 with wrangler
`4.128.0`).

The offload leaves the tree ready to deploy, whatever happens. A file it
cannot move (no bucket declared, credentials missing, an upload error, an HTML
page) is deleted from the tree with an error annotation that says how to serve
it from R2, and the command exits `1`. The Docs workflow publishes everything
else first, then fails the job: the dropped file's links are dead, and a green
run would hide that. A silent trim once hid such a 404 for three years.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import mimetypes
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import quote

from click_extra import echo

from .cloudflare import (
    CloudflareError,
    CloudflareHTTPError,
    _account,
    _call,
    _token,
    _zone_for,
)
from .github.actions import AnnotationLevel, ReportAction, emit_annotation
from .hashing import compute_file_sha256
from .humanize import format_file_size
from .pages_redirects import parse_redirects
from .tabular import render_markdown_table

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from typing import IO, Final

PAGES_MAX_FILE_SIZE: Final = 25 * 1024 * 1024
"""Largest file Cloudflare Pages Direct Upload accepts, in bytes."""

S3_MAX_OBJECT_SIZE: Final = 5 * 1024**3 - 5 * 1024**2
"""Largest object one S3 `PUT` writes to R2, in bytes: 5 MiB short of 5 GiB.

A bigger file needs a multipart upload, which this module does not implement.
"""

ACCESS_KEY_ID_ENV: Final = "CLOUDFLARE_R2_ACCESS_KEY_ID"
"""Environment variable holding the bucket-scoped S3 access key ID."""

CACHE_CONTROL: Final = "public, max-age=31536000, immutable"
"""Cache header of every uploaded object. Its key names its bytes, so it never
changes."""

EMPTY_PAYLOAD_SHA256: Final = hashlib.sha256(b"").hexdigest()
"""Payload hash of a request without a body, like `HEAD`."""

MIN_TLS: Final = "1.2"
"""TLS floor of the bucket's custom domain. R2 defaults to `1.0`."""

OFFLOAD_DOCS_URL: Final = "https://repomatic.net/cloudflare#files-over-25-mib"
"""Where a message about a dropped file or a missing key sends the reader to set
up R2."""

OFFLOAD_DOCS_LINK: Final = f"[big file offloading]({OFFLOAD_DOCS_URL})"
"""{data}`OFFLOAD_DOCS_URL` as the Markdown link that ends a dropped file's reason."""

PAGE_SUFFIXES: Final = frozenset((".htm", ".html"))
"""Extensions of pages, which stay out of the bucket. See the module
docstring."""

REDIRECT_STATUS: Final = 302
"""Status of the generated redirects. See the module docstring."""

REDIRECTS_HEADER: Final = (
    "# Files over the 25 MiB Cloudflare Pages limit, served from R2."
    " Written by `repomatic cloudflare-r2 --offload`."
)
"""Comment line above the generated rules in the built `_redirects`."""

S3_REGION: Final = "auto"
"""Region of the signature's credential scope. R2 has no regions."""

S3_SERVICE: Final = "s3"
"""Service of the signature's credential scope."""

S3_TIMEOUT: Final = 300
"""Socket timeout in seconds. An upload of hundreds of megabytes takes a
while."""

SECRET_ACCESS_KEY_ENV: Final = "CLOUDFLARE_R2_SECRET_ACCESS_KEY"
"""Environment variable holding the bucket-scoped S3 secret access key."""

_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
"""A Markdown link, captured as its label and its URL."""

_MIME_TYPES = mimetypes.MimeTypes()
"""Python's own extension table, without the host's `mime.types` files: a
content type must not depend on the runner that uploaded the file."""


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sign_v4(
    method: str,
    path: str,
    headers: Mapping[str, str],
    payload_sha256: str,
    *,
    access_key_id: str,
    secret_access_key: str,
    amz_date: str,
    region: str = S3_REGION,
    service: str = S3_SERVICE,
) -> str:
    """Sign one request with AWS Signature Version 4.

    *headers* is the complete set of headers to sign, `host` and `x-amz-date`
    included. The signer encodes the path once, and never normalizes it: S3
    reads `//` and `.` segments literally. The query string stays empty,
    because no request here has one.

    :param method: HTTP method.
    :param path: Request path, not encoded yet.
    :param headers: Names and values of the headers to sign.
    :param payload_sha256: Hex SHA-256 of the request body.
    :param access_key_id: Access key ID.
    :param secret_access_key: Secret access key.
    :param amz_date: Time of the request, as `YYYYMMDDTHHMMSSZ`.
    :param region: Region of the credential scope.
    :param service: Service of the credential scope.
    :return: The value of the `Authorization` header.
    """
    canonical = {
        name.lower(): " ".join(value.split()) for name, value in headers.items()
    }
    names = sorted(canonical)
    signed_headers = ";".join(names)
    canonical_request = "\n".join((
        method,
        quote(path, safe="/"),
        "",
        *(f"{name}:{canonical[name]}" for name in names),
        "",
        signed_headers,
        payload_sha256,
    ))
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join((
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ))
    key = f"AWS4{secret_access_key}".encode()
    for part in (amz_date[:8], region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope},"
        f" SignedHeaders={signed_headers}, Signature={signature}"
    )


def _send(request: urllib.request.Request, label: str) -> None:
    """Send *request*, turning an HTTP refusal into a Cloudflare error.

    The one network call of the S3 half, which the tests replace. *label*
    names the request in an error instead of its URL, which embeds the
    account ID.
    """
    try:
        with urllib.request.urlopen(request, timeout=S3_TIMEOUT):
            return
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        msg = f"{label} failed: HTTP {error.code}\n{detail}".rstrip()
        raise CloudflareHTTPError(msg, error.code) from error
    # `URLError` covers a refused connection, but a timeout or a reset in the
    # middle of a long upload arrives as a bare `OSError` or `HTTPException`.
    # Converting all of them keeps the offload's promise to leave the tree
    # ready to deploy.
    except (OSError, http.client.HTTPException) as error:
        msg = f"{label} failed: {getattr(error, 'reason', error)}"
        raise CloudflareError(msg) from error


@dataclass(frozen=True)
class R2Bucket:
    """One R2 bucket, reached through the S3 API with a bucket-scoped key pair."""

    account_id: str
    """Account that holds the bucket. The S3 endpoint embeds it."""

    name: str
    """Bucket name."""

    access_key_id: str
    """S3 access key ID of an `Object Read & Write` token."""

    secret_access_key: str
    """S3 secret access key of the same token."""

    @property
    def host(self) -> str:
        """Host of the account's S3 endpoint."""
        return f"{self.account_id}.r2.cloudflarestorage.com"

    def _request(
        self,
        method: str,
        key: str,
        payload_sha256: str,
        headers: Mapping[str, str] | None = None,
        body: IO[bytes] | None = None,
    ) -> urllib.request.Request:
        """Build a signed path-style request for object *key*."""
        path = f"/{self.name}/{key}"
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        signed = {
            **(headers or {}),
            "host": self.host,
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": amz_date,
        }
        authorization = sign_v4(
            method,
            path,
            signed,
            payload_sha256,
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            amz_date=amz_date,
        )
        return urllib.request.Request(
            f"https://{self.host}{quote(path, safe='/')}",
            data=body,
            method=method,
            headers={**signed, "authorization": authorization},
        )

    def exists(self, key: str) -> bool:
        """Whether the bucket holds an object at *key*."""
        try:
            _send(
                self._request("HEAD", key, EMPTY_PAYLOAD_SHA256),
                f"HEAD {self.name}/{key}",
            )
        except CloudflareHTTPError as error:
            if error.status == 404:
                return False
            raise
        return True

    def put(self, key: str, path: Path, payload_sha256: str) -> None:
        """Upload file *path* to *key*, with its content type and cache header.

        The body streams from the file with its length declared: without the
        length, `urllib` switches to chunked encoding, which S3 refuses on a
        signed request.
        """
        content_type = (
            _MIME_TYPES.guess_type(path.name)[0] or "application/octet-stream"
        )
        headers = {
            "cache-control": CACHE_CONTROL,
            "content-length": str(path.stat().st_size),
            "content-type": content_type,
        }
        with path.open("rb") as body:
            _send(
                self._request("PUT", key, payload_sha256, headers=headers, body=body),
                f"PUT {self.name}/{key}",
            )


@dataclass(frozen=True)
class Offload:
    """What happened to one file over the Pages limit."""

    path: str
    """Path the site served the file at, like `/downloads/atlas.zip`."""

    size: int
    """Size in bytes."""

    action: ReportAction
    """`UPLOADED`, `SKIPPED` when the bucket already held it, or `DROPPED`."""

    url: str = ""
    """Where the file is served from now. Empty when dropped."""

    reason: str = ""
    """Why the file was dropped, in Markdown. Empty otherwise."""


def oversized_files(root: Path) -> list[Path]:
    """Files under *root* that Cloudflare Pages Direct Upload rejects."""
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.stat().st_size > PAGES_MAX_FILE_SIZE
    )


def _refusal(path: Path, size: int, bucket: R2Bucket | None, unavailable: str) -> str:
    """Why *path* cannot move to *bucket*, or an empty string when it can."""
    if path.suffix.lower() in PAGE_SUFFIXES:
        return (
            "it is a page, and its relative links would resolve against the R2"
            " host. Shrink the page or split it"
        )
    if bucket is None:
        return unavailable
    if size > S3_MAX_OBJECT_SIZE:
        return (
            f"it is over the {format_file_size(S3_MAX_OBJECT_SIZE)} a single S3"
            " upload takes. Upload it by hand in parts, and link it absolutely"
        )
    return ""


def _merge_redirects(target: Path, rules: Sequence[str]) -> str:
    """Put *rules* at the top of *target*, and say why not if they do not fit.

    The merged file must keep every rule the site already had, and hold every
    new one: the engine drops what it dislikes without a word, so the replica
    decides before anything is written.

    :return: An empty string once written, else the reason nothing was.
    """
    before = target.read_text(encoding="UTF-8") if target.exists() else ""
    block = "\n".join((REDIRECTS_HEADER, *rules))
    after = f"{block}\n\n{before}" if before.strip() else f"{block}\n"
    old, new = parse_redirects(before), parse_redirects(after)
    if (
        len(new.invalid) != len(old.invalid)
        or len(new.rules) != len(old.rules) + len(rules)
        or (new.aborted_at_line is None) != (old.aborted_at_line is None)
    ):
        return (
            f"adding {len(rules)} rule(s) to {target.name} would break rules the"
            " site already has, so the redirects were not written"
        )
    target.write_text(after, encoding="UTF-8")
    return ""


def offload(
    root: Path,
    files: Sequence[Path],
    *,
    bucket: R2Bucket | None,
    domain: str,
    unavailable: str = "",
) -> list[Offload]:
    """Move *files* out of the built tree *root*, to *bucket* where possible.

    Every file leaves the tree: Pages cannot take it either way. The ones the
    bucket holds get a rule in `root/_redirects`.

    :param root: Built site, as `wrangler pages deploy` would upload it.
    :param files: Files under *root* over the Pages limit.
    :param bucket: Where the files go. `None` drops them all.
    :param domain: Host that serves *bucket*.
    :param unavailable: Why *bucket* is `None`, repeated as each file's reason.
    :return: One entry per file, in the order of *files*.
    """
    results: list[Offload] = []
    rules: list[str] = []
    for path in files:
        site_path = "/" + path.relative_to(root).as_posix()
        size = path.stat().st_size
        reason = _refusal(path, size, bucket, unavailable)
        if reason or bucket is None:
            results.append(
                Offload(site_path, size, ReportAction.DROPPED, reason=reason)
            )
            path.unlink()
            continue
        try:
            digest = compute_file_sha256(path)
            key = f"{digest}/{path.name}"
            if bucket.exists(key):
                action = ReportAction.SKIPPED
            else:
                bucket.put(key, path, digest)
                action = ReportAction.UPLOADED
        except (CloudflareError, OSError) as error:
            first_line = str(error).splitlines()[0]
            results.append(
                Offload(
                    site_path,
                    size,
                    ReportAction.DROPPED,
                    reason=f"the upload failed ({first_line})",
                )
            )
        else:
            url = f"https://{domain}/{quote(key, safe='/')}"
            results.append(Offload(site_path, size, action, url=url))
            rules.append(f"{quote(site_path, safe='/')} {url} {REDIRECT_STATUS}")
        path.unlink()

    if rules:
        problem = _merge_redirects(root / "_redirects", rules)
        if problem:
            results = [
                entry
                if entry.action is ReportAction.DROPPED
                else replace(entry, action=ReportAction.DROPPED, url="", reason=problem)
                for entry in results
            ]
    return results


def _spell_out_links(markdown: str) -> str:
    """Rewrite each Markdown link in *markdown* as its label and its URL.

    A reason is written once, in the Markdown the step summary renders. An
    annotation shows its text as is, so `[label](url)` becomes `label (url)`
    there. Code spans read the same either way, and stay.
    """
    return _MARKDOWN_LINK.sub(r"\1 (\2)", markdown)


def _report(results: Sequence[Offload]) -> None:
    """Print each outcome, annotate each drop, and fill the step summary."""
    for entry in results:
        size = format_file_size(entry.size)
        if entry.action is ReportAction.DROPPED:
            emit_annotation(
                AnnotationLevel.ERROR,
                f"{entry.path} ({size}) is over the 25 MiB Cloudflare Pages"
                " limit, so this deploy dropped it and its links are dead:"
                f" {_spell_out_links(entry.reason)}",
            )
        else:
            echo(f"ok    {entry.path} ({size}) now redirects to {entry.url}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    table = render_markdown_table(
        ("File", "Size", "Result", "Served from"),
        (
            (
                f"`{entry.path}`",
                format_file_size(entry.size),
                entry.action.value,
                entry.url or f"Nowhere: {entry.reason}",
            )
            for entry in results
        ),
        align=("left", "right", "left", "left"),
    )
    with open(summary, "a", encoding="UTF-8") as stream:
        stream.write(f"### Files over the Cloudflare Pages size limit\n\n{table}\n")


def _bucket_from_environment(bucket: str, project: str) -> tuple[R2Bucket | None, str]:
    """The bucket to upload to, or `None` and the reason it is out of reach."""
    if not bucket:
        return None, (
            "`[tool.repomatic] site.cloudflare-r2-bucket` and"
            f" `site.cloudflare-r2-domain` missing for {OFFLOAD_DOCS_LINK}"
        )
    missing = [
        f"`{name}`"
        for name in (ACCESS_KEY_ID_ENV, SECRET_ACCESS_KEY_ENV)
        if not os.getenv(name)
    ]
    if missing:
        return None, f"{' and '.join(missing)} missing for {OFFLOAD_DOCS_LINK}"
    try:
        account = _account(_token(), project)
    except (CloudflareError, OSError) as error:
        return None, f"the account did not resolve ({str(error).splitlines()[0]})"
    return (
        R2Bucket(
            account_id=account,
            name=bucket,
            access_key_id=os.environ[ACCESS_KEY_ID_ENV],
            secret_access_key=os.environ[SECRET_ACCESS_KEY_ENV],
        ),
        "",
    )


def run_offload(root: Path, *, bucket: str, domain: str, project: str) -> int:
    """Move every file over the Pages limit out of *root*.

    Credentials and the account resolve only when a file needs them, so a site
    with nothing oversized makes no network call.

    :param root: Built site, before `wrangler pages deploy` uploads it.
    :param bucket: `site.cloudflare-r2-bucket`, empty to drop every file.
    :param domain: `site.cloudflare-r2-domain`.
    :param project: Pages project, which resolves the account.
    :return: `1` when a file was dropped, else `0`. The tree is ready to deploy
        either way.
    """
    files = oversized_files(root)
    if not files:
        echo(f"ok    no file in {root} is over the 25 MiB Cloudflare Pages limit.")
        return 0
    target, unavailable = _bucket_from_environment(bucket, project)
    results = offload(
        root, files, bucket=target, domain=domain, unavailable=unavailable
    )
    _report(results)
    dropped = sum(entry.action is ReportAction.DROPPED for entry in results)
    if dropped:
        echo(f"\n{dropped} file(s) dropped. The rest of {root} is ready to deploy.")
        return 1
    return 0


def _custom_domain(base: str, bucket: str, domain: str, token: str, fix: bool) -> int:
    """Attach *domain* to the bucket with the TLS floor, or report its drift.

    :return: `1` when drift or a manual step remains, else `0`.
    """
    listed = (_call(f"{base}/domains/custom", token) or {}).get("domains") or []
    entry = next((item for item in listed if item.get("domain") == domain), None)
    if entry is None:
        if not fix:
            echo(f"DRIFT {domain} is not attached to bucket {bucket!r}.")
            return 1
        zone = _zone_for(domain, token)
        if zone is None:
            echo(
                f"note  could not read the zone of {domain}, so it is not"
                " attached. Attach it by hand: wrangler r2 bucket domain add"
                f" {bucket} --domain {domain} --zone-id {{zone-id}}"
                f" --min-tls {MIN_TLS}"
            )
            return 1
        _call(
            f"{base}/domains/custom",
            token,
            method="POST",
            body={
                "domain": domain,
                "zoneId": zone["id"],
                "enabled": True,
                "minTLS": MIN_TLS,
            },
        )
        echo(
            f"ok    attached {domain} to bucket {bucket!r}, with TLS {MIN_TLS} at"
            " least. Its certificate issues within minutes: --check shows it."
        )
        return 0

    wanted = {"enabled": True, "minTLS": MIN_TLS}
    # A domain attached without a floor reads back with no `minTLS` at all,
    # which R2 serves as 1.0.
    live = {
        "enabled": bool(entry.get("enabled")),
        "minTLS": entry.get("minTLS") or "1.0",
    }
    status = entry.get("status") or {}
    progress = f"ownership={status.get('ownership')} ssl={status.get('ssl')}"
    if live == wanted:
        echo(
            f"ok    {domain} serves the bucket, with TLS {MIN_TLS} at least"
            f" ({progress})."
        )
        return 0
    if not fix:
        echo(f"DRIFT {domain}: live={live} want={wanted} ({progress})")
        return 1
    _call(f"{base}/domains/custom/{domain}", token, method="PUT", body=wanted)
    echo(f"ok    set {domain} to {wanted}.")
    return 0


def _managed_domain(base: str, token: str, fix: bool) -> int:
    """Turn off the bucket's `r2.dev` URL, or report that it is on.

    Cloudflare rate-limits that URL and meant it for development. It also
    serves the bucket without the custom domain's TLS floor.

    :return: `1` when the URL is on and stays on, else `0`.
    """
    managed = _call(f"{base}/domains/managed", token) or {}
    if not managed.get("enabled"):
        echo("ok    the r2.dev URL is off.")
        return 0
    if not fix:
        echo(
            f"DRIFT the r2.dev URL {managed.get('domain')} is on. It serves the"
            " bucket under a development rate limit, without the custom"
            " domain's TLS floor."
        )
        return 1
    _call(f"{base}/domains/managed", token, method="PUT", body={"enabled": False})
    echo("ok    turned the r2.dev URL off.")
    return 0


def run_cloudflare_r2(
    project: str, bucket: str, domain: str, *, check: bool = False, create: bool = False
) -> int:
    """Create or check the bucket that serves the site's oversized files.

    Exactly one of *check* and *create* must be set; the CLI enforces that.

    :param project: Pages project of the site, which resolves the account.
    :param bucket: `site.cloudflare-r2-bucket`.
    :param domain: `site.cloudflare-r2-domain`.
    :param check: Report drift from the declared state, and exit `1` on any.
    :param create: Create the bucket when missing, attach the domain with the
        TLS floor, and turn the `r2.dev` URL off. An existing bucket is reused
        and brought to the same state, so a re-run is safe.
    :return: Exit code: `0` when the bucket matches the declared state, `1`
        on drift or on a step left to do by hand.
    """
    token = _token()
    account = _account(token, project)
    base = f"/accounts/{account}/r2/buckets/{bucket}"

    try:
        _call(base, token)
    except CloudflareHTTPError as error:
        if error.status != 404:
            raise
        if check:
            echo(
                f"DRIFT bucket {bucket!r} does not exist. Create it with"
                " `repomatic cloudflare-r2 --create`."
            )
            return 1
        _call(
            f"/accounts/{account}/r2/buckets",
            token,
            method="POST",
            body={"name": bucket},
        )
        echo(f"ok    created bucket {bucket!r}.")
    else:
        echo(f"ok    bucket {bucket!r} exists.")

    drift = _custom_domain(base, bucket, domain, token, fix=create)
    drift += _managed_domain(base, token, fix=create)

    if create:
        echo(
            "\nUploads from CI need a key pair limited to this bucket. Create an"
            f" account API token with Object Read & Write on {bucket!r} only."
            f" Store its Access Key ID as {ACCESS_KEY_ID_ENV} and its Secret"
            f" Access Key as {SECRET_ACCESS_KEY_ENV}, in the repository secrets:"
            f" see {OFFLOAD_DOCS_URL}"
        )
    return 1 if drift else 0
