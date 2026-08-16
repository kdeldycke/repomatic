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

"""Tests for the Cloudflare Pages drift reconciliation.

Everything runs against a mocked `_call`: the module's value is the diffing,
merging and credential-resolution logic, none of which needs a live account to
prove.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from repomatic import cloudflare
from repomatic.cloudflare import (
    CloudflareError,
    Setting,
    _account,
    _diff,
    _merge,
    _nest,
    _redact,
    _token,
    _token_expiry_warning,
    desired_settings,
    run_cloudflare_pages,
)

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
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account-under-test")


def _api(project_state, calls=None):
    """A `_call` stand-in serving *project_state* and recording writes.

    Token-verify endpoints answer like an account credential with no expiry
    recorded, which keeps the expiry warning out of tests not about it.
    """

    def call(path, token, method="GET", body=None):
        if calls is not None:
            calls.append((method, path, body))
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


def test_nest_and_merge_build_one_patch_body():
    body: dict = {}
    _merge(body, _nest(("deployment_configs", "production", "compatibility_date"), "d"))
    _merge(
        body, _nest(("deployment_configs", "production", "placement", "mode"), "smart")
    )
    _merge(body, _nest(("deployment_configs", "preview", "compatibility_date"), "d"))
    assert body == {
        "deployment_configs": {
            "production": {
                "compatibility_date": "d",
                "placement": {"mode": "smart"},
            },
            "preview": {"compatibility_date": "d"},
        }
    }


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
    assert _token() == "from-env"


def test_token_falls_back_to_wrangler_login(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    config = tmp_path / "default.toml"
    config.write_text('oauth_token = "from-wrangler"\n', encoding="UTF-8")
    monkeypatch.setattr(cloudflare, "WRANGLER_CONFIG_PATHS", (config,))
    assert _token() == "from-wrangler"


def test_token_refuses_an_expired_wrangler_session(monkeypatch, tmp_path):
    """A stale local login must name itself, not 403 somewhere downstream.

    wrangler refreshes its own token transparently and nothing here does, so
    a session left overnight hands Cloudflare a dead credential. Left to the
    API, that arrives as a bare `403` on whichever call runs first, which
    reads as a scope problem and sends the reader auditing permissions that
    were never wrong.
    """
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    stale = (datetime.now(timezone.utc) - timedelta(hours=15)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    config = tmp_path / "default.toml"
    config.write_text(
        f'oauth_token = "stale"\nexpiration_time = "{stale}"\n', encoding="UTF-8"
    )
    monkeypatch.setattr(cloudflare, "WRANGLER_CONFIG_PATHS", (config,))
    with pytest.raises(CloudflareError, match="expired on"):
        _token()


def test_token_accepts_a_live_wrangler_session(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    fresh = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    config = tmp_path / "default.toml"
    config.write_text(
        f'oauth_token = "live"\nexpiration_time = "{fresh}"\n', encoding="UTF-8"
    )
    monkeypatch.setattr(cloudflare, "WRANGLER_CONFIG_PATHS", (config,))
    assert _token() == "live"


def test_token_tolerates_a_session_with_no_recorded_expiry(monkeypatch, tmp_path):
    """An unreadable or absent expiry must not block a working credential."""
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    config = tmp_path / "default.toml"
    config.write_text(
        'oauth_token = "undated"\nexpiration_time = "whenever"\n', encoding="UTF-8"
    )
    monkeypatch.setattr(cloudflare, "WRANGLER_CONFIG_PATHS", (config,))
    assert _token() == "undated"


def test_token_absence_names_both_remedies(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr(
        cloudflare, "WRANGLER_CONFIG_PATHS", (tmp_path / "missing.toml",)
    )
    with pytest.raises(CloudflareError, match="wrangler login"):
        _token()


def test_account_prefers_the_environment(credentials):
    """An explicit account ID skips the lookup entirely."""
    with patch.object(cloudflare, "_call") as call:
        assert _account("token") == "account-under-test"
    assert not call.called


def test_account_resolves_a_lone_account_from_the_token(monkeypatch):
    """The ordinary case: one account, so nothing needs configuring.

    A token scoped to nothing but `Cloudflare Pages: Edit` still enumerates
    the account it belongs to, which is what lets CI carry the token alone.
    """
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    with patch.object(cloudflare, "_call", return_value=[{"id": "solo", "name": "P"}]):
        assert _account("token") == "solo"


@pytest.mark.parametrize(
    ("accounts", "fragment"),
    (
        pytest.param([], "sees no account at all", id="none-visible"),
        pytest.param(
            [{"id": "a", "name": "Personal"}, {"id": "b", "name": "Work"}],
            r"sees 2 accounts \(Personal, Work\)",
            id="ambiguous",
        ),
    ),
)
def test_account_refuses_to_guess(monkeypatch, accounts, fragment):
    """Anything but one account fails loudly, naming what it saw.

    Deploying into a guessed account is worse than not deploying, and the two
    ways the guess can fail want different remedies: an empty answer points
    at a token made under the wrong account, several point at a credential
    that has to be told which.
    """
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    with (
        patch.object(cloudflare, "_call", return_value=accounts),
        pytest.raises(CloudflareError, match=fragment),
    ):
        _account("token")


def test_check_clean_project_exits_zero(credentials, capsys):
    with patch.object(cloudflare, "_call", side_effect=_api(CLEAN_PROJECT)):
        exit_code = run_cloudflare_pages(
            PROJECT,
            check=True,
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
    with patch.object(cloudflare, "_call", side_effect=_api(stale)):
        exit_code = run_cloudflare_pages(
            PROJECT,
            check=True,
            compatibility_date="2026-06-16",
            placement="smart",
        )
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "DRIFT" in output
    assert "live='2023-03-01' want='2026-06-16'" in output
    assert "1 drifted setting(s)." in output


def test_check_flags_a_reattached_git_source_as_read_only(credentials, capsys):
    """The one drift `--apply` must not touch: a source block means a second
    publisher, and unpicking that belongs in the dashboard, deliberately."""
    reattached = {**CLEAN_PROJECT, "source": {"type": "github"}}
    with patch.object(cloudflare, "_call", side_effect=_api(reattached)):
        exit_code = run_cloudflare_pages(PROJECT, apply=True)
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
    with patch.object(cloudflare, "_call", side_effect=_api(stale, calls)):
        exit_code = run_cloudflare_pages(
            PROJECT,
            apply=True,
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


def test_account_id_prints_the_bare_value_and_touches_no_project(credentials, capsys):
    """It pipes into `gh secret set`, so nothing may surround the value.

    It also has to answer before any project exists, since storing the account
    ID is a prerequisite of the very first deploy.
    """
    calls: list = []
    with patch.object(cloudflare, "_call", side_effect=_api(CLEAN_PROJECT, calls)):
        exit_code = run_cloudflare_pages("", account_id=True)
    assert exit_code == 0
    assert capsys.readouterr().out == "account-under-test\n"
    assert not calls


def _domain_api(attached, records, zones=None, calls=None, dns_write_fails=False):
    """A `_call` stand-in for the attach-domain paths, recording writes."""
    zones = [{"id": "zone-id", "name": "example.com"}] if zones is None else zones

    def call(path, token, method="GET", body=None):
        if calls is not None:
            calls.append((method, path, body))
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
        cloudflare, "_call", side_effect=_domain_api(attached, records, calls=calls)
    ):
        exit_code = run_cloudflare_pages(PROJECT, attach_domain="example.com")
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
        cloudflare, "_call", side_effect=_domain_api(attached, records, calls=calls)
    ):
        exit_code = run_cloudflare_pages(PROJECT, attach_domain="example.com")
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
        cloudflare,
        "_call",
        side_effect=_domain_api([], [], calls=None, dns_write_fails=True),
    ):
        exit_code = run_cloudflare_pages(PROJECT, attach_domain="example.com")
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "cannot write DNS" in output
    assert "proxied CNAME named example.com" in output


def test_attach_domain_finds_the_zone_above_a_subdomain(credentials, capsys):
    """`docs.example.com` lives in `example.com`, so labels get stripped."""
    records: list = []
    with patch.object(cloudflare, "_call", side_effect=_domain_api([], records)):
        exit_code = run_cloudflare_pages(PROJECT, attach_domain="docs.example.com")
    assert exit_code == 0
    assert records[0]["name"] == "docs.example.com"


def test_attach_domain_survives_a_credential_that_cannot_read_zones(
    credentials, capsys
):
    """A deploy token scoped to Pages alone carries no `Zone → Read`."""
    with patch.object(cloudflare, "_call", side_effect=_domain_api([], [], zones=[])):
        exit_code = run_cloudflare_pages(PROJECT, attach_domain="example.com")
    assert exit_code == 1
    assert "could not read the zone" in capsys.readouterr().out


def test_dump_redacts_and_exits_zero(credentials, capsys):
    leaky = {**CLEAN_PROJECT, "oauth_token": "leaked"}
    with patch.object(cloudflare, "_call", side_effect=_api(leaky)):
        exit_code = run_cloudflare_pages(PROJECT, dump=True)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "<redacted>" in output
    assert "leaked" not in output


def test_create_posts_the_project_then_applies(credentials, capsys):
    calls: list = []
    with patch.object(cloudflare, "_call", side_effect=_api(CLEAN_PROJECT, calls)):
        exit_code = run_cloudflare_pages(PROJECT, create=True)
    assert exit_code == 0
    assert calls[0] == (
        "POST",
        "/accounts/account-under-test/pages/projects",
        {"name": PROJECT, "production_branch": "main"},
    )
    assert f"Created Direct Upload project {PROJECT!r}." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("days_away", "warns"),
    (
        pytest.param(5, True, id="five-days-out"),
        pytest.param(200, False, id="far-away"),
    ),
)
def test_token_expiry_warning_counts_down(days_away, warns):
    """Cloudflare never warns about expiry, so the drift check has to."""
    expires = (
        datetime.now(timezone.utc) + timedelta(days=days_away, hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def call(path, token, method="GET", body=None):
        if path == "/accounts/orchard/tokens/verify":
            # The account endpoint rejects a user-owned token, and the
            # fallback must absorb that rather than fail the run.
            raise CloudflareError("HTTP 401")
        return {"id": "token-id", "expires_on": expires}

    with patch.object(cloudflare, "_call", side_effect=call):
        warning = _token_expiry_warning("token", "orchard")
    if warns:
        assert warning is not None
        assert "Rotate now" in warning
    else:
        assert warning is None


def test_token_expiry_warning_never_fails_the_run():
    """A credential that verifies nowhere still deploys fine: silence, not error."""

    def call(path, token, method="GET", body=None):
        raise CloudflareError("HTTP 401")

    with patch.object(cloudflare, "_call", side_effect=call):
        assert _token_expiry_warning("token", "orchard") is None
