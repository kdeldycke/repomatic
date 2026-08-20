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

"""Tests for the `[tool.repomatic]` schema, focused on ecosystem flavors."""

from __future__ import annotations

from dataclasses import MISSING, fields as dc_fields

import pytest
from extra_platforms import ALL_AGENTS, ALL_CI

from repomatic import config as config_mod
from repomatic.config import (
    AGENT_LAYOUTS,
    DEFAULT_AGENT,
    DEFAULT_CI,
    SITE_DEPLOY_TARGETS,
    AgentLayout,
    Config,
    FlavorConfig,
    config_reference,
    load_repomatic_config,
)

# -- Vocabulary ---------------------------------------------------------------


def test_supported_agents_are_real_extra_platforms_traits():
    """Every agent repomatic lays out must exist upstream.

    The whole point of borrowing extra-platforms' vocabulary is that repomatic
    never invents an ID of its own, so a typo here fails loudly.
    """
    known = {trait.id for trait in ALL_AGENTS}
    assert set(AGENT_LAYOUTS) <= known, (
        f"not extra-platforms agent IDs: {set(AGENT_LAYOUTS) - known}"
    )


def test_flavor_defaults_are_real_extra_platforms_traits():
    """Both defaults must be drawn from the upstream vocabularies."""
    assert DEFAULT_AGENT in {trait.id for trait in ALL_AGENTS}
    assert DEFAULT_CI in {trait.id for trait in ALL_CI}


def test_flavor_defaults():
    """An unconfigured project targets Claude Code on GitHub Actions."""
    flavor = FlavorConfig()
    assert flavor.agent == DEFAULT_AGENT
    assert flavor.ci == DEFAULT_CI


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("agent", "claude-code", "claude_code"),
        ("agent", "  Claude_Code  ", "claude_code"),
        ("ci", "github-ci", "github_ci"),
        ("ci", "GITHUB_CI", "github_ci"),
    ),
)
def test_flavor_normalizes_spelling(field, value, expected):
    """Hyphens, case and padding all resolve to the upstream trait ID."""
    assert getattr(FlavorConfig(**{field: value}), field) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    (("agent", "papaya"), ("ci", "mango"), ("agent", ""), ("ci", "kiwi_ci")),
)
def test_flavor_rejects_unknown_trait(field, value):
    """A value outside the upstream vocabulary reads as a typo."""
    with pytest.raises(ValueError, match="Unknown"):
        FlavorConfig(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"), (("agent", "cursor"), ("agent", "cline"), ("ci", "gitlab_ci"))
)
def test_flavor_rejects_unsupported_ecosystem(field, value):
    """A real ecosystem repomatic cannot target says so, not "unknown"."""
    with pytest.raises(ValueError, match="Unsupported"):
        FlavorConfig(**{field: value})


# -- Derived asset locations --------------------------------------------------


CURSOR_LAYOUT = AgentLayout(
    skills="./.cursor/skills/",
    subagents="./.cursor/agents/",
    settings="./.cursor/settings.json",
)
"""Stand-in layout for an agent repomatic does not target, used to prove the
flavor actually drives the locations rather than the Claude Code defaults
coinciding with them."""


def test_default_locations_come_from_the_agent_layout():
    """The layout table is the single source of truth for every default."""
    config = Config()
    layout = AGENT_LAYOUTS[DEFAULT_AGENT]
    assert config.skills_location == layout.skills
    assert config.subagents_location == layout.subagents
    assert config.settings_location == layout.settings


def test_locations_follow_the_agent_flavor(monkeypatch):
    """Selecting another agent moves the assets to that agent's layout."""
    monkeypatch.setitem(config_mod.AGENT_LAYOUTS, "cursor", CURSOR_LAYOUT)
    config = Config(flavor=FlavorConfig(agent="cursor"))
    assert config.skills_location == "./.cursor/skills/"
    assert config.subagents_location == "./.cursor/agents/"
    assert config.settings_location == "./.cursor/settings.json"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        pytest.param(
            {"skills_location": "./custom/skills/"},
            {
                "skills_location": "./custom/skills/",
                "subagents_location": "./.cursor/agents/",
                "settings_location": "./.cursor/settings.json",
            },
            id="skills",
        ),
        pytest.param(
            {"subagents_location": "./custom/agents/"},
            {
                "skills_location": "./.cursor/skills/",
                "subagents_location": "./custom/agents/",
                "settings_location": "./.cursor/settings.json",
            },
            id="subagents",
        ),
        pytest.param(
            {"settings_location": "./custom/settings.json"},
            {
                "skills_location": "./.cursor/skills/",
                "subagents_location": "./.cursor/agents/",
                "settings_location": "./custom/settings.json",
            },
            id="settings",
        ),
    ),
)
def test_explicit_locations_override_the_flavor(monkeypatch, overrides, expected):
    """An explicit location outranks whatever the agent prefers.

    Each location is checked on its own, so overriding one never drags the
    others off the flavor's layout.
    """
    monkeypatch.setitem(config_mod.AGENT_LAYOUTS, "cursor", CURSOR_LAYOUT)
    config = Config(flavor=FlavorConfig(agent="cursor"), **overrides)
    for field_name, value in expected.items():
        assert getattr(config, field_name) == value


def test_load_repomatic_config_defaults(tmp_path, monkeypatch):
    """Test that load_repomatic_config returns a Config instance with defaults."""
    monkeypatch.chdir(tmp_path)
    config = load_repomatic_config()
    assert isinstance(config, Config)
    assert config.dependency_graph.output == "./docs/assets/dependencies.mmd"
    assert config.dependency_graph.all_groups is True
    assert config.dependency_graph.all_extras is True
    assert config.dependency_graph.no_groups == []
    assert config.dependency_graph.no_extras == []
    assert config.dependency_graph.level is None
    assert config.nuitka_enabled is True
    assert config.nuitka_entry_points == []
    assert config.labels.extra_files == []
    assert config.pypi_package_history == []
    assert config.setup_guide is True
    assert config.workflow.sync is True
    assert config.exclude == []
    assert config.include == []


def test_load_repomatic_config_custom_values(tmp_path, monkeypatch):
    """Test that load_repomatic_config reads custom values from pyproject.toml."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic]
dependency-graph.output = "./custom/deps.mmd"
nuitka.enabled = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    config = load_repomatic_config()
    assert config.dependency_graph.output == "./custom/deps.mmd"
    assert config.nuitka_enabled is False


def test_load_repomatic_config_with_preloaded_data():
    """Test that load_repomatic_config accepts pre-parsed pyproject data."""
    data = {
        "tool": {
            "repomatic": {
                "dependency-graph": {"output": "./custom/deps.mmd"},
            },
        },
    }
    config = load_repomatic_config(data)
    assert config.dependency_graph.output == "./custom/deps.mmd"
    # Other defaults are still present.
    assert config.gitignore.location == "./.gitignore"


@pytest.mark.parametrize("target", sorted(SITE_DEPLOY_TARGETS))
def test_site_deploy_accepts_every_implemented_target(target):
    """Each target names a deploy job in `docs.yaml`, so each must load."""
    assert (
        load_repomatic_config({
            "tool": {"repomatic": {"site": {"deploy": target}}}
        }).site_deploy
        == target
    )


def test_site_deploy_rejects_a_target_with_no_job_behind_it():
    """An unimplemented target would publish nowhere, from a green workflow.

    The `docs.yaml` jobs gate on equality with a known value, so a typo or a
    host nobody wired up simply matches neither, and the run reports success
    having deployed nothing at all.
    """
    with pytest.raises(ValueError, match="Unsupported site.deploy 'netlify'"):
        load_repomatic_config({"tool": {"repomatic": {"site": {"deploy": "netlify"}}}})


def test_site_cloudflare_settings_load_from_their_table():
    """The drift-check declarations ride the same `site.*` table."""
    config = load_repomatic_config({
        "tool": {
            "repomatic": {
                "site": {
                    "deploy": "cloudflare-pages",
                    "cloudflare-project": "papaya-site",
                    "cloudflare-compatibility-date": "2026-06-16",
                    "cloudflare-placement": "smart",
                }
            }
        }
    })
    assert config.site_deploy == "cloudflare-pages"
    assert config.site_cloudflare_project == "papaya-site"
    assert config.site_cloudflare_compatibility_date == "2026-06-16"
    assert config.site_cloudflare_placement == "smart"


def test_site_cloudflare_placement_rejects_unknown_modes():
    """A bad placement mode must die here, not in a Cloudflare PATCH response.

    The value is written verbatim to the live project by `cloudflare-pages
    --apply`, so the API rejection would surface far from the
    `pyproject.toml` line that caused it.
    """
    with pytest.raises(
        ValueError, match="Unsupported site.cloudflare-placement 'clever'"
    ):
        load_repomatic_config({
            "tool": {"repomatic": {"site": {"cloudflare-placement": "clever"}}}
        })


def test_load_repomatic_config_warns_unknown_keys(tmp_path, monkeypatch, caplog):
    """Unknown keys in [tool.repomatic] produce a warning, not an error."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic]
nonexistent-option = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    config = load_repomatic_config()
    assert isinstance(config, Config)
    # The warning comes from click-extra's schema layer (warn_unknown), which
    # names keys in their normalized snake_case form.
    assert "Unknown configuration option(s): nonexistent_option" in caplog.text


def test_load_repomatic_config_warns_unknown_keys_once(tmp_path, monkeypatch, caplog):
    """Re-loading the same project reports its unknown keys only once.

    Every helper needing a setting re-loads the config, so without the
    per-project dedup a single stale key floods each invocation's output.
    """
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[tool.repomatic]
nonexistent-option = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    load_repomatic_config()
    load_repomatic_config()
    assert caplog.text.count("Unknown configuration option(s)") == 1


def test_config_reference():
    """Config reference table covers all Config fields with descriptions."""
    rows = config_reference()

    # Count expected rows: one per flat Config field, plus sub-fields for
    # nested dataclass fields (which are expanded, not listed as a single row).
    expected_rows = 0
    for f in dc_fields(Config):
        default = f.default_factory() if f.default_factory is not MISSING else f.default
        if hasattr(default, "__dataclass_fields__"):
            expected_rows += len(dc_fields(type(default)))  # type: ignore[arg-type]
        else:
            expected_rows += 1
    assert len(rows) == expected_rows

    # Every row has a non-empty description.
    for option, ftype, default, desc in rows:
        assert desc, f"Empty description for {option}"
