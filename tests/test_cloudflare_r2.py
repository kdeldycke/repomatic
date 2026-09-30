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

"""Tests for serving oversized site files from Cloudflare R2.

The signer runs against AWS's published Signature Version 4 vectors. Everything
else runs against stand-ins for the two network seams, `_send` for the S3 API
and `_call` for the REST API: the module's value is in the decisions it makes,
none of which needs a live bucket to prove.
"""

from __future__ import annotations

import hashlib
from typing import cast
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic import cloudflare_r2
from repomatic.cli.main import repomatic
from repomatic.cloudflare import CloudflareError, CloudflareHTTPError
from repomatic.cloudflare_r2 import (
    ACCESS_KEY_ID_ENV,
    CACHE_CONTROL,
    PAGES_MAX_FILE_SIZE,
    REDIRECTS_HEADER,
    SECRET_ACCESS_KEY_ENV,
    R2Bucket,
    _bucket_from_environment,
    offload,
    oversized_files,
    run_cloudflare_r2,
    run_offload,
    sign_v4,
)
from repomatic.github.actions import ReportAction
from repomatic.pages_redirects import MAX_STATIC_RULES, parse_redirects

DOMAIN = "files.example.com"

OVERSIZED = PAGES_MAX_FILE_SIZE + 1
"""One byte past what Direct Upload accepts."""


def _sparse(path, size=OVERSIZED):
    """Create *path* at *size* bytes without writing them, so tests stay fast."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)
    return path


# ----- The signer, against AWS's own test suite
#
# From awslabs/aws-c-auth, tests/aws-signing-test-suite/v4, with the suite's
# shared credentials, region, service and timestamp. Each case pins the
# `Authorization` header AWS published for it.

AWS_SUITE = {
    "access_key_id": "AKIDEXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    "amz_date": "20150830T123600Z",
    "region": "us-east-1",
    "service": "service",
}

EMPTY = hashlib.sha256(b"").hexdigest()
FORM = hashlib.sha256(b"Param1=value1").hexdigest()


@pytest.mark.parametrize(
    ("method", "path", "headers", "payload", "expected"),
    (
        pytest.param(
            "GET",
            "/",
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            EMPTY,
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
            "aws4_request, SignedHeaders=host;x-amz-date, Signature="
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
            id="get-vanilla",
        ),
        pytest.param(
            "POST",
            "/",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Host": "example.amazonaws.com",
                "Content-Length": "13",
                "X-Amz-Date": "20150830T123600Z",
                "x-amz-content-sha256": FORM,
            },
            FORM,
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
            "aws4_request, SignedHeaders=content-length;content-type;host;"
            "x-amz-content-sha256;x-amz-date, Signature="
            "d3875051da38690788ef43de4db0d8f280229d82040bfac253562e56c3f20e0b",
            id="post-x-www-form-urlencoded",
        ),
        pytest.param(
            "GET",
            "/example space/",
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            EMPTY,
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
            "aws4_request, SignedHeaders=host;x-amz-date, Signature="
            "652487583200325589f1fba4c7e578f72c47cb61beeca81406b39ddec1366741",
            id="get-space-unnormalized",
        ),
        pytest.param(
            "GET",
            "/ሴ",
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            EMPTY,
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
            "aws4_request, SignedHeaders=host;x-amz-date, Signature="
            "8318018e0b0f223aa2bbf98705b62bb787dc9c0e678f255a891fd03141be5d85",
            id="get-utf8",
        ),
    ),
)
def test_sign_v4_matches_the_aws_test_suite(method, path, headers, payload, expected):
    assert sign_v4(method, path, headers, payload, **AWS_SUITE) == expected


def test_sign_v4_depends_on_the_secret():
    """Control: the vectors above would pass a signer that ignored the key."""
    headers = {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}
    genuine = sign_v4("GET", "/", headers, EMPTY, **AWS_SUITE)
    forged = sign_v4(
        "GET", "/", headers, EMPTY, **{**AWS_SUITE, "secret_access_key": "papaya"}
    )
    assert genuine != forged


# ----- The S3 half: requests to one bucket

BUCKET = R2Bucket(
    account_id="account-under-test",
    name="papaya-files",
    access_key_id="key-id-under-test",
    secret_access_key="secret-under-test",
)


def test_request_is_path_style_and_signs_the_s3_headers():
    request = BUCKET._request("HEAD", "abc/my atlas.zip", EMPTY)
    assert request.full_url == (
        "https://account-under-test.r2.cloudflarestorage.com"
        "/papaya-files/abc/my%20atlas.zip"
    )
    authorization = request.get_header("Authorization")
    assert authorization is not None
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=key-id-under-test/")
    assert "/auto/s3/aws4_request," in authorization
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date," in authorization
    assert "secret-under-test" not in authorization


@pytest.mark.parametrize(
    ("raised", "expected"),
    ((None, True), (CloudflareHTTPError("HEAD failed: HTTP 404", 404), False)),
)
def test_exists_reads_a_404_as_absence(raised, expected):
    with patch.object(cloudflare_r2, "_send", side_effect=raised):
        assert BUCKET.exists("abc/atlas.zip") is expected


def test_exists_lets_a_refusal_through():
    """A `403` is a credential problem, not a missing object: it must not read
    as "upload it", since the upload would be refused the same way."""
    denied = CloudflareHTTPError("HEAD failed: HTTP 403", 403)
    with (
        patch.object(cloudflare_r2, "_send", side_effect=denied),
        pytest.raises(CloudflareHTTPError),
    ):
        BUCKET.exists("abc/atlas.zip")


@pytest.mark.parametrize(
    "raised",
    (
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param(ConnectionResetError("reset"), id="reset"),
    ),
)
def test_send_turns_a_broken_connection_into_a_cloudflare_error(raised):
    """A timeout mid-upload is no `URLError`: it must still reach the offload
    as an error it catches, or the deploy fails."""
    request = BUCKET._request("HEAD", "abc/atlas.zip", EMPTY)
    with (
        patch.object(cloudflare_r2.urllib.request, "urlopen", side_effect=raised),
        pytest.raises(CloudflareError, match=r"HEAD papaya-files/abc failed"),
    ):
        cloudflare_r2._send(request, "HEAD papaya-files/abc")


def test_put_streams_the_file_with_its_type_length_and_cache_header(tmp_path):
    path = tmp_path / "atlas.zip"
    path.write_bytes(b"papaya")
    sent = []
    with patch.object(
        cloudflare_r2, "_send", side_effect=lambda request, label: sent.append(request)
    ):
        BUCKET.put("abc/atlas.zip", path, "abc")
    (request,) = sent
    assert request.get_method() == "PUT"
    assert request.get_header("Content-length") == "6"
    assert request.get_header("Content-type") == "application/zip"
    assert request.get_header("Cache-control") == CACHE_CONTROL
    signed = request.get_header("Authorization")
    assert "cache-control;content-length;content-type;host;" in signed


def test_put_falls_back_to_a_byte_stream_for_an_unknown_format(tmp_path):
    path = tmp_path / "printer.rfu"
    path.write_bytes(b"\x00")
    sent = []
    with patch.object(
        cloudflare_r2, "_send", side_effect=lambda request, label: sent.append(request)
    ):
        BUCKET.put("abc/printer.rfu", path, "abc")
    assert sent[0].get_header("Content-type") == "application/octet-stream"


# ----- The offload: moving files out of a built tree


class FakeBucket:
    """An `R2Bucket` stand-in holding objects in memory."""

    def __init__(self, held=(), fail=None):
        self.objects = set(held)
        self.fail = fail
        self.puts = []

    def exists(self, key):
        return key in self.objects

    def put(self, key, path, payload_sha256):
        if self.fail:
            raise self.fail
        assert hashlib.sha256(path.read_bytes()).hexdigest() == payload_sha256
        self.puts.append(key)
        self.objects.add(key)


def _offload(root, files, bucket):
    """Run the offload against a stand-in, which it types as an `R2Bucket`."""
    return offload(root, files, bucket=cast("R2Bucket", bucket), domain=DOMAIN)


def test_oversized_files_skips_what_pages_accepts(tmp_path):
    big = _sparse(tmp_path / "downloads" / "atlas.zip")
    _sparse(tmp_path / "downloads" / "lemon.zip", PAGES_MAX_FILE_SIZE)
    (tmp_path / "index.html").write_text("<p>kiwi</p>", encoding="UTF-8")
    assert oversized_files(tmp_path) == [big]


def test_offload_uploads_and_redirects(tmp_path):
    big = _sparse(tmp_path / "downloads" / "atlas.zip")
    digest = hashlib.sha256(big.read_bytes()).hexdigest()
    bucket = FakeBucket()

    (result,) = _offload(tmp_path, [big], bucket)

    key = f"{digest}/atlas.zip"
    assert bucket.puts == [key]
    assert result.action is ReportAction.UPLOADED
    assert result.url == f"https://{DOMAIN}/{key}"
    assert not big.exists()
    redirects = (tmp_path / "_redirects").read_text(encoding="UTF-8")
    assert redirects == (
        f"{REDIRECTS_HEADER}\n/downloads/atlas.zip https://{DOMAIN}/{key} 302\n"
    )


def test_offload_reuses_an_object_the_bucket_holds(tmp_path):
    """A content-addressed key that exists already holds these exact bytes."""
    big = _sparse(tmp_path / "atlas.zip")
    key = f"{hashlib.sha256(big.read_bytes()).hexdigest()}/atlas.zip"
    bucket = FakeBucket(held={key})

    (result,) = _offload(tmp_path, [big], bucket)

    assert result.action is ReportAction.SKIPPED
    assert bucket.puts == []
    assert f"/atlas.zip https://{DOMAIN}/{key} 302" in (
        tmp_path / "_redirects"
    ).read_text(encoding="UTF-8")


def test_offload_puts_its_rules_above_the_existing_ones(tmp_path):
    """Above the first dynamic rule is the only place an exact rule is free."""
    existing = "/old /new 301\n/blog/* /posts/:splat 301\n"
    (tmp_path / "_redirects").write_text(existing, encoding="UTF-8")
    big = _sparse(tmp_path / "atlas.zip")

    _offload(tmp_path, [big], FakeBucket())

    merged = (tmp_path / "_redirects").read_text(encoding="UTF-8")
    assert merged.startswith(REDIRECTS_HEADER)
    assert merged.endswith(existing)
    parsed = parse_redirects(merged)
    assert [rule.source for rule in parsed.rules] == ["/atlas.zip", "/old", "/blog/*"]
    assert not parsed.invalid


def test_offload_encodes_paths_in_rules(tmp_path):
    """A space would split the rule into fields."""
    big = _sparse(tmp_path / "city maps" / "atlas.zip")
    _offload(tmp_path, [big], FakeBucket())
    parsed = parse_redirects((tmp_path / "_redirects").read_text(encoding="UTF-8"))
    assert parsed.rules[0].source == "/city%20maps/atlas.zip"


def test_offload_without_a_bucket_drops_every_file(tmp_path):
    big = _sparse(tmp_path / "atlas.zip")

    (result,) = offload(
        tmp_path, [big], bucket=None, domain="", unavailable="no bucket here"
    )

    assert result.action is ReportAction.DROPPED
    assert result.reason == "no bucket here"
    assert not big.exists()
    assert not (tmp_path / "_redirects").exists()


def test_offload_keeps_pages_out_of_the_bucket(tmp_path):
    page = _sparse(tmp_path / "reference" / "index.html")
    bucket = FakeBucket()

    (result,) = _offload(tmp_path, [page], bucket)

    assert result.action is ReportAction.DROPPED
    assert "relative links" in result.reason
    assert bucket.puts == []
    assert not page.exists()


def test_offload_drops_a_file_whose_upload_fails(tmp_path):
    big = _sparse(tmp_path / "atlas.zip")
    bucket = FakeBucket(fail=CloudflareError("PUT papaya-files/x failed: HTTP 403"))

    (result,) = _offload(tmp_path, [big], bucket)

    assert result.action is ReportAction.DROPPED
    assert "HTTP 403" in result.reason
    assert not big.exists()
    assert not (tmp_path / "_redirects").exists()


def test_offload_refuses_to_break_existing_rules(tmp_path):
    """A full static budget cannot take one more rule without losing its last."""
    full = "".join(f"/fig-{index} /kiwi 301\n" for index in range(MAX_STATIC_RULES))
    (tmp_path / "_redirects").write_text(full, encoding="UTF-8")
    big = _sparse(tmp_path / "atlas.zip")

    (result,) = _offload(tmp_path, [big], FakeBucket())

    assert result.action is ReportAction.DROPPED
    assert result.url == ""
    assert "would break rules" in result.reason
    assert (tmp_path / "_redirects").read_text(encoding="UTF-8") == full


# ----- Credentials and the entry point


def test_bucket_from_environment_needs_a_declared_bucket():
    bucket, reason = _bucket_from_environment("", "papaya-site")
    assert bucket is None
    assert "site.cloudflare-r2-bucket" in reason


def test_bucket_from_environment_names_the_missing_secrets(monkeypatch):
    monkeypatch.delenv(ACCESS_KEY_ID_ENV, raising=False)
    monkeypatch.setenv(SECRET_ACCESS_KEY_ENV, "secret-under-test")
    bucket, reason = _bucket_from_environment("papaya-files", "papaya-site")
    assert bucket is None
    assert reason == f"{ACCESS_KEY_ID_ENV} not set"


def test_bucket_from_environment_resolves_the_account(monkeypatch):
    monkeypatch.setenv(ACCESS_KEY_ID_ENV, "key-id-under-test")
    monkeypatch.setenv(SECRET_ACCESS_KEY_ENV, "secret-under-test")
    with (
        patch.object(cloudflare_r2, "_token", return_value="token"),
        patch.object(cloudflare_r2, "_account", return_value="account-under-test"),
    ):
        bucket, reason = _bucket_from_environment("papaya-files", "papaya-site")
    assert reason == ""
    assert bucket == BUCKET


def test_run_offload_touches_no_credential_when_nothing_is_oversized(tmp_path):
    (tmp_path / "index.html").write_text("<p>kiwi</p>", encoding="UTF-8")
    with patch.object(
        cloudflare_r2, "_bucket_from_environment", side_effect=AssertionError
    ):
        assert (
            run_offload(tmp_path, bucket="papaya-files", domain=DOMAIN, project="p")
            == 0
        )


def test_run_offload_writes_the_step_summary(tmp_path, monkeypatch, capsys):
    site = tmp_path / "site"
    _sparse(site / "atlas.zip")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert run_offload(site, bucket="", domain="", project="papaya-site") == 0

    assert "::warning::/atlas.zip (" in capsys.readouterr().out
    table = summary.read_text(encoding="UTF-8")
    assert "| `/atlas.zip` |" in table
    assert ReportAction.DROPPED.value in table


# ----- The REST half: creating and checking the bucket

BASE = "/accounts/account-under-test/r2/buckets/papaya-files"


def _r2_api(state, calls):
    """A `_call` stand-in serving one bucket's *state* and recording writes."""

    def call(path, token, method="GET", body=None):
        calls.append((method, path, body))
        if method != "GET":
            return {}
        if path == BASE:
            if not state.get("exists", True):
                raise CloudflareHTTPError("GET bucket failed: HTTP 404", 404)
            return {"name": "papaya-files"}
        if path == f"{BASE}/domains/custom":
            return {"domains": state.get("domains", [])}
        if path == f"{BASE}/domains/managed":
            return {"enabled": state.get("r2_dev", False), "domain": "pub-x.r2.dev"}
        raise AssertionError(path)

    return call


ATTACHED = {
    "domain": DOMAIN,
    "enabled": True,
    "minTLS": "1.2",
    "status": {"ownership": "active", "ssl": "active"},
}


def _run(state, calls, *, zone=None, **mode):
    with (
        patch.object(cloudflare_r2, "_token", return_value="token"),
        patch.object(cloudflare_r2, "_account", return_value="account-under-test"),
        patch.object(cloudflare_r2, "_zone_for", return_value=zone),
        patch.object(cloudflare_r2, "_call", side_effect=_r2_api(state, calls)),
    ):
        return run_cloudflare_r2("papaya-site", "papaya-files", DOMAIN, **mode)


def test_check_passes_a_bucket_in_the_declared_state(capsys):
    calls: list = []
    assert _run({"domains": [ATTACHED]}, calls, check=True) == 0
    assert all(method == "GET" for method, _path, _body in calls)
    assert "DRIFT" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "state",
    (
        pytest.param({"exists": False}, id="no-bucket"),
        pytest.param({"domains": []}, id="no-domain"),
        pytest.param({"domains": [{**ATTACHED, "minTLS": None}]}, id="tls-1.0"),
        pytest.param({"domains": [{**ATTACHED, "enabled": False}]}, id="disabled"),
        pytest.param({"domains": [ATTACHED], "r2_dev": True}, id="r2-dev-on"),
    ),
)
def test_check_reports_drift_and_writes_nothing(state, capsys):
    calls: list = []
    assert _run(state, calls, check=True) == 1
    assert all(method == "GET" for method, _path, _body in calls)
    assert "DRIFT" in capsys.readouterr().out


def test_create_builds_everything_from_nothing():
    calls: list = []
    state = {"exists": False, "r2_dev": True}
    assert _run(state, calls, zone={"id": "zone-under-test"}, create=True) == 0
    writes = [(method, path, body) for method, path, body in calls if method != "GET"]
    assert writes == [
        ("POST", "/accounts/account-under-test/r2/buckets", {"name": "papaya-files"}),
        (
            "POST",
            f"{BASE}/domains/custom",
            {
                "domain": DOMAIN,
                "zoneId": "zone-under-test",
                "enabled": True,
                "minTLS": "1.2",
            },
        ),
        ("PUT", f"{BASE}/domains/managed", {"enabled": False}),
    ]


def test_create_raises_the_tls_floor_of_an_attached_domain():
    calls: list = []
    state = {"domains": [{**ATTACHED, "minTLS": "1.0"}]}
    assert _run(state, calls, create=True) == 0
    assert (
        "PUT",
        f"{BASE}/domains/custom/{DOMAIN}",
        {
            "enabled": True,
            "minTLS": "1.2",
        },
    ) in calls


def test_create_converges_without_writing_twice():
    calls: list = []
    assert _run({"domains": [ATTACHED]}, calls, create=True) == 0
    assert all(method == "GET" for method, _path, _body in calls)


def test_create_names_the_manual_step_when_the_zone_is_unreadable(capsys):
    calls: list = []
    assert _run({"domains": []}, calls, create=True) == 1
    assert "wrangler r2 bucket domain add papaya-files" in capsys.readouterr().out


# ----- The command line


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A checkout declaring nothing, so this repository's own config stays out."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "papaya"\n', encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "args",
    (
        pytest.param([], id="none"),
        pytest.param(["--check", "--create"], id="two"),
    ),
)
def test_cli_wants_exactly_one_mode(project_dir, args):
    result = CliRunner().invoke(repomatic, ["cloudflare-r2", *args])
    assert result.exit_code == 2
    assert "Pick exactly one of" in result.output


def test_cli_check_needs_a_declared_bucket(project_dir):
    result = CliRunner().invoke(repomatic, ["cloudflare-r2", "--check"])
    assert result.exit_code == 2
    assert "No bucket declared" in result.output


def test_cli_offload_drops_without_a_bucket(project_dir):
    site = project_dir / "site"
    _sparse(site / "atlas.zip")
    result = CliRunner().invoke(
        repomatic, ["cloudflare-r2", "--offload", str(site), "--project", "papaya"]
    )
    assert result.exit_code == 0
    assert "was dropped from the deploy" in result.output
    assert not (site / "atlas.zip").exists()
