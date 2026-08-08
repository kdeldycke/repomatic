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

import pytest
from extra_platforms import ALL_AGENTS, ALL_CI

from repomatic import config as config_mod
from repomatic.config import (
    AGENT_LAYOUTS,
    DEFAULT_AGENT,
    DEFAULT_CI,
    AgentLayout,
    Config,
    FlavorConfig,
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
    agents="./.cursor/agents/",
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
    assert config.agents_location == layout.agents
    assert config.settings_location == layout.settings


def test_locations_follow_the_agent_flavor(monkeypatch):
    """Selecting another agent moves the assets to that agent's layout."""
    monkeypatch.setitem(config_mod.AGENT_LAYOUTS, "cursor", CURSOR_LAYOUT)
    config = Config(flavor=FlavorConfig(agent="cursor"))
    assert config.skills_location == "./.cursor/skills/"
    assert config.agents_location == "./.cursor/agents/"
    assert config.settings_location == "./.cursor/settings.json"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        pytest.param(
            {"skills_location": "./custom/skills/"},
            {
                "skills_location": "./custom/skills/",
                "agents_location": "./.cursor/agents/",
                "settings_location": "./.cursor/settings.json",
            },
            id="skills",
        ),
        pytest.param(
            {"agents_location": "./custom/agents/"},
            {
                "skills_location": "./.cursor/skills/",
                "agents_location": "./custom/agents/",
                "settings_location": "./.cursor/settings.json",
            },
            id="agents",
        ),
        pytest.param(
            {"settings_location": "./custom/settings.json"},
            {
                "skills_location": "./.cursor/skills/",
                "agents_location": "./.cursor/agents/",
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
