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

"""Tests for the Cloudflare API client and the Pages drift reconciliation.

Everything runs against a stand-in for `api.call`, the one seam of the REST
API: the value is in the diffing, merging and credential-resolution logic, none
of which needs a live account to prove. The transport tests stub one layer
lower, at the `urlopen` that {func}`repomatic.http.request` calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.client import RemoteDisconnected
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from click.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.cloudflare import api
from repomatic.cloudflare.api import (
    WRANGLER_CONFIG_FILE,
    CloudflareError,
    CloudflareHTTPError,
    Status,
    account,
    api_token,
    print_status,
    resolve_project,
    token_expiry_warning,
    wrangler_config_paths,
)
from repomatic.cloudflare.pages import (
    PagesMode,
    Setting,
    _diff,
    _patch_body,
    _redact,
    desired_settings,
    run_cloudflare_pages,
)
from repomatic.config import Config, publish_config
from tests.conftest import FakeResponse

PROJECT = "papaya-site"

CLEAN_PROJECT = {
    "name": PROJECT,
    "source": None,
    "build_config": {"build_command": ""},
    "deployment_configs": {
        "production": {
            "compatibility_date": "2026-06-16",
            "placement": {"mode": "smart"},
            "build_image_major_version": 3,
        },
        "preview": {
            "compatibility_date": "2026-06-16",
            "placement": {"mode": "smart"},
            "build_image_major_version": 3,
        },
    },
}


@pytest.fixture
def credentials(monkeypatch):
    """Environment credentials, so no code path goes looking for wrangler's."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token-under-test")


SOLO_ACCOUNT = [{"id": "account-under-test", "name": "Test"}]
"""What `/accounts` answers in the stand-ins: one account, so the
credential resolves its target without any project probing."""


def _api(project_state, calls=None):
    """An `api.call` stand-in serving *project_state* and recording writes.

    Token-verify endpoints answer like an account credential with no expiry
    recorded, which keeps the expiry warning out of tests not about it.
    """

    def call(path, token, method="GET", body=None):
        if calls is not None:
            calls.append((method, path, body))
        if path == "/accounts":
            return SOLO_ACCOUNT
        if path.endswith("/tokens/verify"):
            return {"id": "token-id", "status": "active"}
        if method == "POST":
            return {"name": PROJECT}
        return project_state

    return call


def test_desired_settings_manage_only_what_is_declared():
    """An empty declaration leaves the live value alone, floors excepted."""
    managed = [setting for setting in desired_settings() if setting.managed]
    # The build image floor is the only always-on managed setting, once per
    # environment.
    assert [setting.path for setting in managed] == [
        ("deployment_configs", "production", "build_image_major_version"),
        ("deployment_configs", "preview", "build_image_major_version"),
    ]
    observed = [setting for setting in desired_settings() if not setting.managed]
    assert [setting.path for setting in observed] == [
        ("source",),
        ("build_config", "build_command"),
    ]


def test_desired_settings_apply_declarations_to_both_environments():
    """Pages has exactly production and preview, and both get every value."""
    settings = desired_settings(compatibility_date="2026-06-16", placement="smart")
    dates = [
        setting.path[1]
        for setting in settings
        if setting.path[-1:] == ("compatibility_date",)
    ]
    assert dates == ["production", "preview"]


def test_diff_reads_a_missing_key_as_the_null_it_wants():
    """Cloudflare reports "unset" as null or as absence, and both must match.

    Without the equivalence, whichever form the API is not currently using
    reads as drift, and `--apply` would PATCH a no-op forever.
    """
    wants_gone = Setting(
        path=("source",), desired=None, default="x", why="", managed=False
    )
    matched, drifted = _diff({}, (wants_gone,))
    assert matched == [wants_gone]
    assert not drifted


def test_diff_accepts_a_freshly_created_project_as_clean():
    """A project created seconds ago carries no `build_config` object at all.

    That absence is the strongest possible statement that nothing builds it,
    so it must not read as drift against the empty string saying the same.
    Reported as drift, `--create` closed by declaring a fault in the project
    it had just made correctly, and pointed at a dashboard field that does
    not exist yet.
    """
    fresh = {
        "name": PROJECT,
        "deployment_configs": {
            "production": {"build_image_major_version": 3},
            "preview": {"build_image_major_version": 3},
        },
    }
    _matched, drifted = _diff(fresh, desired_settings())
    assert not drifted


@pytest.mark.parametrize(
    "live",
    (
        pytest.param({}, id="absent"),
        pytest.param({"build_config": {}}, id="empty-parent"),
        pytest.param({"build_config": {"build_command": None}}, id="null"),
        pytest.param({"build_config": {"build_command": ""}}, id="empty-string"),
    ),
)
def test_diff_treats_every_spelling_of_unset_alike(live):
    """Which form arrives depends on the field and the project's age."""
    wants_empty = Setting(
        path=("build_config", "build_command"),
        desired="",
        default="",
        why="",
        managed=False,
    )
    matched, drifted = _diff(live, (wants_empty,))
    assert matched == [wants_empty]
    assert not drifted


def test_diff_still_catches_a_real_build_command():
    """The equivalence must not swallow a project someone reconnected."""
    wants_empty = Setting(
        path=("build_config", "build_command"),
        desired="",
        default="",
        why="",
        managed=False,
    )
    _matched, drifted = _diff(
        {"build_config": {"build_command": "npm run build"}}, (wants_empty,)
    )
    assert drifted == [(wants_empty, "npm run build")]


def test_diff_reports_live_value_next_to_wanted_one():
    setting = Setting(
        path=("deployment_configs", "production", "compatibility_date"),
        desired="2026-06-16",
        default="x",
        why="because",
    )
    matched, drifted = _diff(
        {"deployment_configs": {"production": {"compatibility_date": "2023-03-01"}}},
        (setting,),
    )
    assert not matched
    assert drifted == [(setting, "2023-03-01")]


def test_patch_body_nests_every_setting_into_one_body():
    settings = (
        Setting(
            ("deployment_configs", "production", "compatibility_date"), "d", "", ""
        ),
        Setting(
            ("deployment_configs", "production", "placement", "mode"), "smart", "", ""
        ),
        Setting(("deployment_configs", "preview", "compatibility_date"), "d", "", ""),
    )
    assert _patch_body(settings) == {
        "deployment_configs": {
            "production": {
                "compatibility_date": "d",
                "placement": {"mode": "smart"},
            },
            "preview": {"compatibility_date": "d"},
        }
    }


def test_setting_describes_its_scope_and_how_sure_its_default_is():
    setting = Setting(("source",), None, "x", "", verified=True, managed=False)
    assert setting.describe() == "source  [read-only, stock default 'x': documented]"


def test_redact_scrubs_every_secret_key():
    redacted = _redact({
        "oauth_token": "secret",
        "nested": [{"api_token": "secret", "name": "fine"}],
    })
    assert redacted == {
        "oauth_token": "<redacted>",
        "nested": [{"api_token": "<redacted>", "name": "fine"}],
    }


def test_token_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "from-env")
    assert api_token() == "from-env"


def test_token_falls_back_to_wrangler_login(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    config = tmp_path / "default.toml"
    config.write_text('oauth_token = "from-wrangler"\n', encoding="UTF-8")
    monkeypatch.setattr(api, "wrangler_config_paths", lambda: (config,))
    assert api_token() == "from-wrangler"


def test_token_refuses_an_expired_wrangler_session(monkeypatch, tmp_path):
    """A stale local login must name itself, not 403 somewhere downstream.

    wrangler refreshes its own token transparently and nothing here does, so
    a session left overnight hands Cloudflare a dead credential. Left to the
    API, that arrives as a bare `403` on whichever call runs first, which
    reads as a scope problem and sends the reader auditing permissions that
    were never wrong.
    """
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    stale = (datetime.now(UTC) - timedelta(hours=15)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config = tmp_path / "default.toml"
    config.write_text(
        f'oauth_token = "stale"\nexpiration_time = "{stale}"\n', encoding="UTF-8"
    )
    monkeypatch.setattr(api, "wrangler_config_paths", lambda: (config,))
    with pytest.raises(CloudflareError, match="expired on"):
        api_token()


def test_token_accepts_a_live_wrangler_session(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    fresh = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config = tmp_path / "default.toml"
    config.write_text(
        f'oauth_token = "live"\nexpiration_time = "{fresh}"\n', encoding="UTF-8"
    )
    monkeypatch.setattr(api, "wrangler_config_paths", lambda: (config,))
    assert api_token() == "live"


def test_token_tolerates_a_session_with_no_recorded_expiry(monkeypatch, tmp_path):
    """An unreadable or absent expiry must not block a working credential."""
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    config = tmp_path / "default.toml"
    config.write_text(
        'oauth_token = "undated"\nexpiration_time = "whenever"\n', encoding="UTF-8"
    )
    monkeypatch.setattr(api, "wrangler_config_paths", lambda: (config,))
    assert api_token() == "undated"


def test_token_absence_names_both_remedies(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr(
        api, "wrangler_config_paths", lambda: (tmp_path / "missing.toml",)
    )
    with pytest.raises(CloudflareError, match="wrangler login"):
        api_token()


def test_account_resolves_a_lone_account_from_theapi_token():
    """The ordinary case: one account, so nothing needs configuring.

    A token scoped to nothing but `Cloudflare Pages: Edit` still enumerates
    the account it belongs to, which is what lets CI carry the token alone.
    """
    with patch.object(api, "call", return_value=[{"id": "solo", "name": "P"}]):
        assert account("token", PROJECT) == "solo"


def _multi_account_api(owners):
    """Serve two accounts, letting only *owners* hold the project."""

    def call(path, token, method="GET", body=None):
        if path == "/accounts":
            return [
                {"id": "personal", "name": "Personal"},
                {"id": "work", "name": "Work"},
            ]
        if path.endswith(f"/pages/projects/{PROJECT}"):
            if path.split("/")[2] in owners:
                return CLEAN_PROJECT
            raise CloudflareError(f"GET {path} failed: HTTP 404")
        raise AssertionError(f"unexpected call: {path}")

    return call


def test_account_picks_the_one_owner_among_several_accounts():
    """A multi-account credential resolves by project ownership.

    The project name is always known to the caller, so asking which account
    holds it picks the account without a stored identifier.
    """
    with patch.object(api, "call", side_effect=_multi_account_api({"work"})):
        assert account("token", PROJECT) == "work"


def test_account_lookup_skips_no_probing_for_a_lone_account():
    """The common path costs one call: no project probing when unneeded."""
    calls: list = []

    def call(path, token, method="GET", body=None):
        calls.append(path)
        return SOLO_ACCOUNT

    with patch.object(api, "call", side_effect=call):
        assert account("token", PROJECT) == "account-under-test"
    assert calls == ["/accounts"]


def test_account_refuses_when_no_account_is_visible():
    """An empty answer points at a token made under the wrong account."""
    with (
        patch.object(api, "call", return_value=[]),
        pytest.raises(CloudflareError, match="sees no account at all"),
    ):
        account("token", PROJECT)


@pytest.mark.parametrize(
    ("owners", "fragment"),
    (
        pytest.param(set(), r"none of them holds a Pages project", id="no-owner"),
        pytest.param({"personal", "work"}, "has no single answer", id="two-owners"),
    ),
)
def test_account_refuses_to_guess_among_several_accounts(owners, fragment):
    """Ownership that has no single answer fails loudly rather than guessing.

    Deploying into a guessed account is worse than not deploying, and the
    remedy for both shapes is the same: a token created under the account
    the project belongs to.
    """
    with (
        patch.object(api, "call", side_effect=_multi_account_api(owners)),
        pytest.raises(CloudflareError, match=fragment),
    ):
        account("token", PROJECT)


def test_check_clean_project_exits_zero(credentials, capsys):
    with patch.object(api, "call", side_effect=_api(CLEAN_PROJECT)):
        exit_code = run_cloudflare_pages(
            PROJECT,
            PagesMode.CHECK,
            compatibility_date="2026-06-16",
            placement="smart",
        )
    assert exit_code == 0
    assert "No drift" in capsys.readouterr().out


def test_check_drifted_project_exits_one_and_explains(credentials, capsys):
    stale = {
        **CLEAN_PROJECT,
        "deployment_configs": {
            "production": {
                "compatibility_date": "2023-03-01",
                "placement": {"mode": "smart"},
                "build_image_major_version": 3,
            },
            "preview": {
                "compatibility_date": "2026-06-16",
                "placement": {"mode": "smart"},
                "build_image_major_version": 3,
            },
        },
    }
    with patch.object(api, "call", side_effect=_api(stale)):
        exit_code = run_cloudflare_pages(
            PROJECT,
            PagesMode.CHECK,
            compatibility_date="2026-06-16",
            placement="smart",
        )
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "DRIFT" in output
    assert "live='2023-03-01' want='2026-06-16'" in output
    assert "1 drifted setting(s)." in output


def test_check_flags_a_reattached_git_source_as_read_only(credentials, capsys):
    """The one drift `--apply` must not touch.

    A source block means a second publisher, and unpicking that belongs in the
    dashboard, deliberately.
    """
    reattached = {**CLEAN_PROJECT, "source": {"type": "github"}}
    with patch.object(api, "call", side_effect=_api(reattached)):
        exit_code = run_cloudflare_pages(PROJECT, PagesMode.APPLY)
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "read-only" in output
    assert "Nothing to apply" in output


def test_apply_patches_only_the_managed_drift(credentials, capsys):
    calls: list = []
    stale = {
        **CLEAN_PROJECT,
        "deployment_configs": {
            "production": {
                "compatibility_date": "2023-03-01",
                "placement": {"mode": "off"},
                "build_image_major_version": 3,
            },
            "preview": {
                "compatibility_date": "2026-06-16",
                "placement": {"mode": "smart"},
                "build_image_major_version": 3,
            },
        },
    }
    with patch.object(api, "call", side_effect=_api(stale, calls)):
        exit_code = run_cloudflare_pages(
            PROJECT,
            PagesMode.APPLY,
            compatibility_date="2026-06-16",
            placement="smart",
        )
    assert exit_code == 0
    patches = [call for call in calls if call[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0][2] == {
        "deployment_configs": {
            "production": {
                "compatibility_date": "2026-06-16",
                "placement": {"mode": "smart"},
            },
        }
    }
    assert "Applied 2 setting(s)." in capsys.readouterr().out


def _domain_api(attached, records, zones=None, calls=None, dns_write_fails=False):
    """An `api.call` stand-in for the attach-domain paths, recording writes."""
    zones = [{"id": "zone-id", "name": "example.com"}] if zones is None else zones

    def call(path, token, method="GET", body=None):
        if calls is not None:
            calls.append((method, path, body))
        if path == "/accounts":
            return SOLO_ACCOUNT
        if "/domains" in path:
            if method == "POST":
                attached.append({"name": body["name"], "status": "pending"})
                return attached[-1]
            return attached
        if path.startswith("/zones?"):
            wanted = path.split("name=")[1]
            return [zone for zone in zones if zone["name"] == wanted]
        if "/dns_records" in path:
            if method == "POST":
                if dns_write_fails:
                    raise CloudflareError("POST failed: HTTP 403")
                records.append({
                    "type": "CNAME",
                    "name": body["name"],
                    "content": body["content"],
                })
                return records[-1]
            return records
        return {}

    return call


def test_attach_domain_registers_the_hostname_and_its_dns(credentials, capsys):
    """The two halves the dashboard does together and the API does not."""
    attached: list = []
    records: list = []
    calls: list = []
    with patch.object(
        api, "call", side_effect=_domain_api(attached, records, calls=calls)
    ):
        exit_code = run_cloudflare_pages(
            PROJECT, PagesMode.ATTACH_DOMAIN, domain="example.com"
        )
    assert exit_code == 0
    assert [entry["name"] for entry in attached] == ["example.com"]
    created = [call for call in calls if call[0] == "POST" and "dns_records" in call[1]]
    assert created[0][2] == {
        "type": "CNAME",
        "name": "example.com",
        "content": f"{PROJECT}.pages.dev",
        "proxied": True,
        "ttl": 1,
        "comment": "Cloudflare Pages custom domain.",
    }
    output = capsys.readouterr().out
    assert "attached example.com" in output
    assert "created CNAME" in output


def test_attach_domain_is_idempotent(credentials, capsys):
    """Re-running after fixing one half must not disturb the other."""
    attached = [{"name": "example.com", "status": "active"}]
    records = [{"type": "CNAME", "name": "example.com", "content": "x.pages.dev"}]
    calls: list = []
    with patch.object(
        api, "call", side_effect=_domain_api(attached, records, calls=calls)
    ):
        exit_code = run_cloudflare_pages(
            PROJECT, PagesMode.ATTACH_DOMAIN, domain="example.com"
        )
    assert exit_code == 0
    assert not [call for call in calls if call[0] == "POST"]
    output = capsys.readouterr().out
    assert "already attached" in output
    assert "already resolves" in output


def test_attach_domain_reports_dns_it_cannot_write(credentials, capsys):
    """A Pages-only token attaches the hostname and cannot finish the job.

    Exits non-zero: a domain resolving to nothing never leaves `pending`, so
    reporting success would describe a site that does not load.
    """
    with patch.object(
        api,
        "call",
        side_effect=_domain_api([], [], calls=None, dns_write_fails=True),
    ):
        exit_code = run_cloudflare_pages(
            PROJECT, PagesMode.ATTACH_DOMAIN, domain="example.com"
        )
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "cannot write DNS" in output
    assert "proxied CNAME named example.com" in output


def test_attach_domain_finds_the_zone_above_a_subdomain(credentials, capsys):
    """`docs.example.com` lives in `example.com`, so labels get stripped."""
    records: list = []
    with patch.object(api, "call", side_effect=_domain_api([], records)):
        exit_code = run_cloudflare_pages(
            PROJECT, PagesMode.ATTACH_DOMAIN, domain="docs.example.com"
        )
    assert exit_code == 0
    assert records[0]["name"] == "docs.example.com"


def test_attach_domain_survives_a_credential_that_cannot_read_zones(
    credentials, capsys
):
    """A deploy token scoped to Pages alone carries no `Zone → Read`."""
    with patch.object(api, "call", side_effect=_domain_api([], [], zones=[])):
        exit_code = run_cloudflare_pages(
            PROJECT, PagesMode.ATTACH_DOMAIN, domain="example.com"
        )
    assert exit_code == 1
    assert "could not read the zone" in capsys.readouterr().out


def test_dump_redacts_and_exits_zero(credentials, capsys):
    leaky = {**CLEAN_PROJECT, "oauth_token": "leaked"}
    with patch.object(api, "call", side_effect=_api(leaky)):
        exit_code = run_cloudflare_pages(PROJECT, PagesMode.DUMP)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "<redacted>" in output
    assert "leaked" not in output


def test_create_posts_the_project_when_missing(credentials, capsys):
    calls: list = []
    created = {"done": False}

    def call(path, token, method="GET", body=None):
        calls.append((method, path, body))
        if path == "/accounts":
            return SOLO_ACCOUNT
        if method == "POST":
            created["done"] = True
            return {"name": PROJECT}
        if path.endswith(f"/pages/projects/{PROJECT}"):
            if not created["done"]:
                raise CloudflareHTTPError(f"GET {path} failed: HTTP 404", 404)
            return CLEAN_PROJECT
        if path.endswith("/tokens/verify"):
            return {"id": "token-id", "status": "active"}
        raise AssertionError(f"unexpected call: {path}")

    with patch.object(api, "call", side_effect=call):
        exit_code = run_cloudflare_pages(PROJECT, PagesMode.CREATE)
    assert exit_code == 0
    posts = [call for call in calls if call[0] == "POST"]
    assert posts[0] == (
        "POST",
        "/accounts/account-under-test/pages/projects",
        {"name": PROJECT, "production_branch": "main"},
    )
    assert f"created Direct Upload project {PROJECT!r}." in capsys.readouterr().out


def test_create_reuses_an_existing_project(credentials, capsys):
    """The API answers 409 on a duplicate name, so a re-run must converge.

    Probing first turns an existing project into the same reconcile path a
    fresh one takes, instead of an error.
    """
    calls: list = []
    with patch.object(api, "call", side_effect=_api(CLEAN_PROJECT, calls)):
        exit_code = run_cloudflare_pages(PROJECT, PagesMode.CREATE)
    assert exit_code == 0
    assert not [call for call in calls if call[0] == "POST"]
    output = capsys.readouterr().out
    assert "already exists" in output
    assert "No drift" in output


def test_create_still_fails_on_a_non_404_refusal(credentials, capsys):
    """A 403 is not 'missing': it must surface, not trigger a POST."""
    calls: list = []

    def call(path, token, method="GET", body=None):
        calls.append((method, path, body))
        if path == "/accounts":
            return SOLO_ACCOUNT
        raise CloudflareHTTPError(f"GET {path} failed: HTTP 403", 403)

    with (
        patch.object(api, "call", side_effect=call),
        pytest.raises(CloudflareHTTPError, match="HTTP 403"),
    ):
        run_cloudflare_pages(PROJECT, PagesMode.CREATE)
    assert not [call for call in calls if call[0] == "POST"]


@pytest.mark.parametrize(
    ("days_away", "warns"),
    (
        pytest.param(5, True, id="five-days-out"),
        pytest.param(200, False, id="far-away"),
    ),
)
def test_token_expiry_warning_counts_down(days_away, warns):
    """Cloudflare never warns about expiry, so the drift check has to."""
    expires = (datetime.now(UTC) + timedelta(days=days_away, hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def call(path, token, method="GET", body=None):
        if path == "/accounts/orchard/tokens/verify":
            # The account endpoint rejects a user-owned token, and the
            # fallback must absorb that rather than fail the run.
            raise CloudflareError("HTTP 401")
        return {"id": "token-id", "expires_on": expires}

    with patch.object(api, "call", side_effect=call):
        warning = token_expiry_warning("token", "orchard")
    if warns:
        assert warning is not None
        assert "Rotate now" in warning
    else:
        assert warning is None


def test_token_expiry_warning_counts_the_hours_of_a_last_day():
    """A token dying today is hours away: whole days would floor that to zero."""
    # A minute of slack: the hour count is truncated, so a bare 5 hours renders
    # as 4 by the time the warning is built.
    expires = (datetime.now(UTC) + timedelta(hours=5, minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def call(path, token, method="GET", body=None):
        return {"id": "token-id", "expires_on": expires}

    with patch.object(api, "call", side_effect=call):
        warning = token_expiry_warning("token", "orchard")
    assert warning is not None
    assert "expires" in warning
    assert "in 5 hours" in warning


def test_token_expiry_warning_never_fails_the_run():
    """A credential that verifies nowhere still deploys fine: silence, not error."""

    def call(path, token, method="GET", body=None):
        raise CloudflareError("HTTP 401")

    with patch.object(api, "call", side_effect=call):
        assert token_expiry_warning("token", "orchard") is None


# ----- The transport


def _http_error(code: int, body: bytes) -> HTTPError:
    """An `HTTPError` carrying *body*, as `urlopen` raises it."""
    return HTTPError(
        "https://api.example.test", code, "Error", Message(), BytesIO(body)
    )


def _envelope(**fields: object) -> FakeResponse:
    """An answer of the REST API, wrapped the way Cloudflare wraps them all."""
    return FakeResponse(json.dumps(fields).encode())


def test_call_returns_the_result_of_the_envelope():
    answer = _envelope(success=True, result=[{"id": "orchard"}])
    with patch("repomatic.http.urlopen", return_value=answer) as mock_open:
        assert api.call("/accounts", "token-under-test") == [{"id": "orchard"}]
    sent = mock_open.call_args.args[0]
    assert sent.get_header("Authorization") == "Bearer token-under-test"
    assert sent.get_header("User-agent").startswith("repomatic/")


def test_call_sends_a_write_as_json():
    with patch(
        "repomatic.http.urlopen", return_value=_envelope(success=True, result={})
    ) as mock_open:
        api.call("/zones", "token", method="POST", body={"name": "example.com"})
    sent = mock_open.call_args.args[0]
    assert sent.get_method() == "POST"
    assert json.loads(sent.data) == {"name": "example.com"}
    assert sent.get_header("Content-type") == "application/json"


@pytest.mark.parametrize(
    "failure",
    (
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param(ConnectionResetError("reset by peer"), id="reset"),
        pytest.param(RemoteDisconnected("Remote end closed connection"), id="dropped"),
    ),
)
def test_call_turns_a_broken_connection_into_a_cloudflare_error(failure):
    """A stalled or dropped connection reaches the command as a caught error.

    It never surfaces as a traceback.
    """
    with (
        patch("repomatic.http.urlopen", side_effect=failure),
        pytest.raises(CloudflareError, match="GET /accounts failed") as excinfo,
    ):
        api.call("/accounts", "token")
    assert not isinstance(excinfo.value, CloudflareHTTPError)


def test_call_keeps_the_status_and_body_of_a_refusal():
    refused = _http_error(404, b'{"errors": [{"code": 8000007}]}')
    with (
        patch("repomatic.http.urlopen", side_effect=refused),
        pytest.raises(CloudflareHTTPError, match="HTTP 404") as excinfo,
    ):
        api.call("/accounts/orchard/pages/projects/papaya", "token")
    assert excinfo.value.status == 404
    assert "8000007" in str(excinfo.value)


def test_call_names_the_likely_cause_of_a_403_on_accounts():
    with (
        patch("repomatic.http.urlopen", side_effect=_http_error(403, b"")),
        pytest.raises(CloudflareHTTPError, match="expired credential"),
    ):
        api.call("/accounts", "token")


@pytest.mark.parametrize(
    ("answer", "fragment"),
    (
        pytest.param(
            _envelope(success=False, errors=[{"code": 7003}]),
            "returned errors",
            id="errors",
        ),
        pytest.param(FakeResponse(b"<html>"), "not JSON", id="not-json"),
    ),
)
def test_call_refuses_an_answer_it_cannot_use(answer, fragment):
    with (
        patch("repomatic.http.urlopen", return_value=answer),
        pytest.raises(CloudflareError, match=fragment),
    ):
        api.call("/accounts", "token")


@pytest.mark.parametrize(
    ("label", "expected"),
    (
        (Status.OK, "ok    fig\n"),
        (Status.DRIFT, "DRIFT fig\n"),
        (None, "      fig\n"),
    ),
)
def test_print_status_aligns_every_message(capsys, label, expected):
    print_status(label, "fig")
    assert capsys.readouterr().out == expected


# ----- Credentials and the project


@pytest.mark.parametrize(
    ("macos", "expected"),
    (
        pytest.param(True, ("home/Library/Preferences", "xdg"), id="macos"),
        pytest.param(False, ("xdg",), id="elsewhere"),
    ),
)
def test_wrangler_config_paths_resolve_when_called(
    monkeypatch, tmp_path, macos, expected
):
    """The paths follow the home and XDG directories of the moment.

    Read at import, they would keep whatever the process saw then, and a
    caller that moves either directory would probe stale paths.
    """
    # `~` expands against HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(api, "is_macos", lambda: macos)
    assert wrangler_config_paths() == tuple(
        tmp_path / parent / WRANGLER_CONFIG_FILE for parent in expected
    )


def test_wrangler_config_paths_default_to_the_xdg_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(api, "is_macos", lambda: False)
    assert wrangler_config_paths() == (tmp_path / ".config" / WRANGLER_CONFIG_FILE,)


@pytest.mark.parametrize(
    ("given", "declared", "expected"),
    (
        ("papaya-site", "kiwi-site", "papaya-site"),
        (None, "kiwi-site", "kiwi-site"),
    ),
)
def test_resolve_project_prefers_the_named_project(given, declared, expected):
    publish_config(Config(site_cloudflare_project=declared))
    assert resolve_project(given) == expected


def test_resolve_project_falls_back_to_the_repository_name(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "orchard/papaya")
    publish_config(Config())
    assert resolve_project() == "papaya"


# ----- The command line


@pytest.mark.parametrize(
    "args",
    (
        pytest.param([], id="none"),
        pytest.param(["--check", "--dump"], id="two"),
    ),
)
def test_cli_wants_exactly_one_mode(args):
    result = CliRunner().invoke(repomatic, ["cloudflare-pages", *args])
    assert result.exit_code == 2
    assert (
        "Pick exactly one of --apply, --attach-domain, --check, --create, --dump."
        in result.output
    )


@pytest.mark.parametrize(
    ("mode", "writes"),
    (
        (PagesMode.APPLY, True),
        (PagesMode.ATTACH_DOMAIN, False),
        (PagesMode.CHECK, False),
        (PagesMode.CREATE, True),
        (PagesMode.DUMP, False),
    ),
)
def test_pages_mode_says_whether_it_writes_the_settings(mode, writes):
    assert mode.writes is writes


def test_cli_needs_a_project_name(monkeypatch):
    monkeypatch.setattr("repomatic.cli.site.resolve_project", lambda project: "")
    result = CliRunner().invoke(repomatic, ["cloudflare-pages", "--check"])
    assert result.exit_code == 2
    assert "No project name: pass --project" in result.output


def test_cli_passes_the_mode_and_the_project(monkeypatch):
    seen: dict = {}

    def run(project, mode, **kwargs):
        seen.update(project=project, mode=mode, **kwargs)
        return 0

    monkeypatch.setattr("repomatic.cli.site.run_cloudflare_pages", run)
    result = CliRunner().invoke(
        repomatic,
        ["cloudflare-pages", "--attach-domain", "example.com", "--project", "fig"],
    )
    assert result.exit_code == 0, result.output
    assert seen["project"] == "fig"
    assert seen["mode"] is PagesMode.ATTACH_DOMAIN
    assert seen["domain"] == "example.com"


def test_cli_reports_a_cloudflare_error_without_a_traceback(monkeypatch):
    def refuse(project, mode, **kwargs):
        msg = "GET /accounts failed: timed out"
        raise CloudflareError(msg)

    monkeypatch.setattr("repomatic.cli.site.run_cloudflare_pages", refuse)
    result = CliRunner().invoke(
        repomatic, ["cloudflare-pages", "--check", "--project", "fig"]
    )
    assert result.exit_code == 1
    assert "GET /accounts failed: timed out" in result.output
    assert "Traceback" not in result.output
