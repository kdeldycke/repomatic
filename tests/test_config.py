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

"""Tests for the `[tool.repomatic]` schema and its loader."""

from __future__ import annotations

from dataclasses import MISSING, fields as dc_fields

import pytest

from repomatic.cli.repo import (
    config_reference_rows,
    format_config_default,
    format_config_type,
)
from repomatic.config import (
    SITE_DEPLOY_TARGETS,
    Config,
    LintRepoConfig,
    MetricsConfig,
    config_key_paths,
    config_reference,
    load_repomatic_config,
    publish_config,
)


def test_default_locations_target_claude_code():
    """Skills, subagents and settings land where Claude Code reads them."""
    config = Config()
    assert config.skills_location == "./.claude/skills/"
    assert config.subagents_location == "./.claude/agents/"
    assert config.settings_location == "./.claude/settings.json"


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    (
        # A key of a nested table.
        pytest.param("metrics.sync", {"metrics": {"sync": True}}, True, id="nested"),
        # A flat field whose dotted path is only metadata.
        pytest.param("debug.sync", {"debug": {"sync": True}}, True, id="flat-dotted"),
        pytest.param(
            "notification.unsubscribe",
            {"notification": {"unsubscribe": True}},
            True,
            id="flat-dotted-other-table",
        ),
        # A flat field named after itself.
        pytest.param("setup-guide", {"setup-guide": False}, False, id="flat"),
        pytest.param(
            "awesome-template.sync",
            {"awesome-template": {"sync": False}},
            False,
            id="flat-dotted-off",
        ),
    ),
)
def test_is_enabled_reads_the_key_the_user_writes(key, value, expected):
    """Every shape of `[tool.repomatic]` key reaches the field that stores it."""
    config = load_repomatic_config({"tool": {"repomatic": value}})
    assert config.is_enabled(key) is expected
    assert Config().is_enabled(key) is not expected


@pytest.mark.parametrize("key", ("metrics.skip", "flavor.agent", "mango.sync", ""))
def test_is_enabled_rejects_a_key_with_no_field(key):
    """A gate wired to a missing key raises instead of reading as switched off."""
    with pytest.raises(ValueError, match="has no"):
        Config().is_enabled(key)


def test_config_key_paths_cover_every_reference_row():
    """The gate map and the reference table agree on the set of keys."""
    assert set(config_key_paths()) == {
        option.strip("`") for option, *_ in config_reference()
    }


@pytest.mark.parametrize(
    "table",
    (
        pytest.param({"flavor": {"agent": "claude_code"}}, id="flavor"),
        pytest.param({"metrics": {"skip": {"papaya": "No forge."}}}, id="metrics-skip"),
    ),
)
def test_removed_keys_warn_as_unknown(table, caplog):
    """A key `8.0.0` removed is an unknown key, reported rather than fatal."""
    config = load_repomatic_config({"tool": {"repomatic": table}})
    assert isinstance(config, Config)
    assert "Unknown configuration option(s)" in caplog.text
    assert not hasattr(config, "flavor")
    assert not hasattr(MetricsConfig(), "skip")


TWO_WEEKS_COOLDOWN = """\
[tool.repomatic]
minimum-release-age = "2 weeks"
"""
"""A `pyproject.toml` whose cooldown differs from the default one."""


def test_published_config_wins_over_the_working_directory(tmp_path, monkeypatch):
    """A published config answers every no-argument load, whatever the file says.

    The CLI resolves its configuration through click-extra (an upward search,
    the user folder, `--config`), and a library helper reading
    `./pyproject.toml` alone would disagree with it.
    """
    (tmp_path / "pyproject.toml").write_text(TWO_WEEKS_COOLDOWN, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    assert load_repomatic_config().minimum_release_age == "2 weeks"

    published = Config(minimum_release_age="3 days")
    publish_config(published)
    assert load_repomatic_config() is published
    assert load_repomatic_config(project_root=tmp_path) is published

    publish_config(None)
    assert load_repomatic_config().minimum_release_age == "2 weeks"


def test_published_config_leaves_another_project_alone(tmp_path, monkeypatch):
    """A project other than the working directory still reads its own file."""
    other = tmp_path / "orchard"
    other.mkdir()
    (other / "pyproject.toml").write_text(TWO_WEEKS_COOLDOWN, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    publish_config(Config(minimum_release_age="3 days"))
    assert load_repomatic_config(project_root=other).minimum_release_age == "2 weeks"


def test_config_holds_plain_data(tmp_path, monkeypatch):
    """A config read from the cached `tomlrt` document holds no `tomlrt` object.

    The document stays shared and keeps its formatting trivia, so the config
    built from it gets plain copies.
    """
    content = """\
[tool.repomatic]
exclude = ["skills"]

[tool.repomatic.labels.file-rules]
"🥭 mango" = ["orchard/**"]
"""
    (tmp_path / "pyproject.toml").write_text(content, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    config = load_repomatic_config()
    assert type(config.exclude) is list
    assert type(config.labels.file_rules) is dict
    assert type(config.labels.file_rules["🥭 mango"]) is list


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
    with pytest.raises(ValueError, match=r"Unsupported site\.deploy 'netlify'"):
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
        ValueError, match=r"Unsupported site\.cloudflare-placement 'clever'"
    ):
        load_repomatic_config({
            "tool": {"repomatic": {"site": {"cloudflare-placement": "clever"}}}
        })


def test_site_cloudflare_r2_keys_load_from_their_table():
    config = load_repomatic_config({
        "tool": {
            "repomatic": {
                "site": {
                    "cloudflare-r2-bucket": "papaya-files",
                    "cloudflare-r2-domain": "files.example.com",
                }
            }
        }
    })
    assert config.site_cloudflare_r2_bucket == "papaya-files"
    assert config.site_cloudflare_r2_domain == "files.example.com"


@pytest.mark.parametrize(
    "site",
    (
        pytest.param({"cloudflare-r2-bucket": "papaya-files"}, id="bucket-alone"),
        pytest.param({"cloudflare-r2-domain": "files.example.com"}, id="domain-alone"),
    ),
)
def test_site_cloudflare_r2_keys_come_as_a_pair(site):
    """A bucket and its host are declared together, or the loader refuses.

    A bucket with nowhere to redirect, or a host with nothing to upload to,
    would only fail at the first oversized file, deep in a deploy.
    """
    with pytest.raises(ValueError, match=r"go together"):
        load_repomatic_config({"tool": {"repomatic": {"site": site}}})


def test_site_cloudflare_r2_domain_must_be_a_bare_hostname():
    """The offload writes `https://{domain}/{key}`: a scheme would double."""
    with pytest.raises(ValueError, match=r"must be a bare hostname"):
        load_repomatic_config({
            "tool": {
                "repomatic": {
                    "site": {
                        "cloudflare-r2-bucket": "papaya-files",
                        "cloudflare-r2-domain": "https://files.example.com",
                    }
                }
            }
        })


def test_lint_repo_declined_keeps_its_keys_verbatim():
    """A declined entry is named by a tool's own keys, so none may be rewritten.

    Key normalization would turn `extend-select` into `extend_select`, which
    names no key of `[tool.ruff]`: the entry would then decline nothing.
    """
    config = load_repomatic_config({
        "tool": {
            "repomatic": {
                "lint-repo": {
                    "declined": {
                        "ruff": {"lint": {"extend-select": {"PLW1514": "Orchard."}}},
                        "pytest": {"addopts": {"--numprocesses=auto": "Harvest."}},
                        "mypy": {"warn_unreachable": "Season."},
                    }
                }
            }
        }
    })
    assert config.lint_repo.declined_entries() == {
        ("ruff", "lint", "extend-select", "PLW1514"): "Orchard.",
        ("pytest", "addopts", "--numprocesses=auto"): "Harvest.",
        ("mypy", "warn_unreachable"): "Season.",
    }


@pytest.mark.parametrize(
    ("declined", "expected"),
    (
        pytest.param({}, {}, id="nothing-declined"),
        pytest.param({"pytest": "Orchard."}, {("pytest",): "Orchard."}, id="tool"),
        pytest.param({"pytest": {}}, {("pytest",): {}}, id="empty-tool-table"),
        pytest.param(
            {"pytest": {"addopts": {}}},
            {("pytest", "addopts"): {}},
            id="empty-key-table",
        ),
        pytest.param(
            {"pytest": {"testpaths": ["tests"]}},
            {("pytest", "testpaths"): ["tests"]},
            id="list",
        ),
    ),
)
def test_lint_repo_declined_entries_keep_what_is_not_a_reason(declined, expected):
    """A leaf holding no reason stays in the answer, for `lint-repo` to report."""
    assert LintRepoConfig(declined=declined).declined_entries() == expected


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


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        ("str", "str"),
        ("str | None", "str"),
        ("list[str]", "list[str]"),
        ("list[dict[str, str]]", "list\\[dict[str, str]\\]"),
        ("dict[str, list[str]] | None", "dict\\[str, list[str]\\]"),
    ],
)
def test_show_config_type_cell(annotation, expected):
    """A nested generic escapes its outer brackets, and `| None` is dropped."""
    assert format_config_type(annotation) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "*(none)*"),
        (True, "`true`"),
        (3, "`3`"),
        ("1 week", '`"1 week"`'),
        ("apple\nkiwi", "*(see example)*"),
        ([], "`[]`"),
        (["apple"], "`['apple']`"),
    ],
)
def test_show_config_default_cell(value, expected):
    """Each default type renders in the cell form the reference table uses."""
    assert format_config_default(value) == expected


def test_show_config_rows_cover_every_key():
    """The rendered table quotes every key the gate map knows, once."""
    options = [row[0] for row in config_reference_rows()]
    assert all(option.startswith("`") and option.endswith("`") for option in options)
    assert sorted(option.strip("`") for option in options) == sorted(config_key_paths())
