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
"""Tests for bundled configuration templates and repository initialization."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from repomatic.config import Config
from repomatic.init_project import (
    EXPORTABLE_FILES,
    RUNTIME_FRAGMENTS,
    _resolve_agents_target,
    _resolve_skills_target,
    _strip_renovate_repo_settings,
    _update_tool_config,
    default_version_pin,
    export_content,
    get_data_content,
    init_config,
    run_init,
)
from repomatic.registry import (
    _BY_NAME,
    ALL_COMPONENTS,
    ALL_WORKFLOW_FILES,
    COMPONENTS,
    REUSABLE_WORKFLOWS,
    SKILL_PHASES,
    BundledComponent,
    GeneratedComponent,
    TemplateComponent,
    ToolConfigComponent,
    WorkflowComponent,
    parse_component_entries,
    valid_file_ids,
)
from repomatic.tool_runner import TOOL_REGISTRY

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


# Convenience set for tests that check opt-in workflow membership.
_OPT_IN_IDS = frozenset(f.file_id for f in _BY_NAME["workflows"].files if f.config_key)


# --- Bundled data and export tests ---


def test_all_component_types_handled() -> None:
    """Verify every component in the registry has a handled type.

    The type-driven dispatch loop in ``run_init`` handles these types.
    If a new component subclass is added without updating the dispatch,
    this test will catch it.
    """
    handled_types = (
        BundledComponent,
        GeneratedComponent,
        TemplateComponent,
        ToolConfigComponent,
        WorkflowComponent,
    )
    for comp in COMPONENTS:
        assert isinstance(comp, handled_types), (
            f"Component {comp.name!r} has unhandled type {type(comp).__name__}"
        )


def test_composite_actions_keep_unmodified() -> None:
    """Composite actions under ``.github/actions/`` must set ``keep_unmodified=True``.

    GitHub Actions resolves ``uses: ./.github/actions/X`` and
    ``uses: owner/repo/.github/actions/X@ref`` directly from the repo path.
    The action file must remain on disk even when byte-identical to the
    bundled default; otherwise the autofix workflow deletes it as
    "redundant" and breaks every caller (upstream and downstream).
    """
    for comp in COMPONENTS:
        if not isinstance(comp, BundledComponent):
            continue
        for entry in comp.files:
            if not entry.target.startswith(".github/actions/"):
                continue
            assert comp.keep_unmodified, (
                f"Component {comp.name!r} ships {entry.target!r} (a composite"
                " action consumed in-place by GitHub Actions) but does not"
                " set `keep_unmodified=True`."
            )


@pytest.mark.parametrize(
    ("scope", "is_awesome", "is_python", "expected"),
    [
        # ALL matches every trait combination.
        ("ALL", False, False, True),
        ("ALL", False, True, True),
        ("ALL", True, False, True),
        ("ALL", True, True, True),
        # AWESOME_ONLY matches awesome repos regardless of Python status.
        ("AWESOME_ONLY", False, False, False),
        ("AWESOME_ONLY", False, True, False),
        ("AWESOME_ONLY", True, False, True),
        ("AWESOME_ONLY", True, True, True),
        # PYTHON_ONLY matches Python repos regardless of awesome status.
        ("PYTHON_ONLY", False, False, False),
        ("PYTHON_ONLY", False, True, True),
        ("PYTHON_ONLY", True, False, False),
        ("PYTHON_ONLY", True, True, True),
    ],
)
def test_repo_scope_matches(
    scope: str, is_awesome: bool, is_python: bool, expected: bool
) -> None:
    """Verify RepoScope.matches returns correct results for all combinations."""
    from repomatic.registry import RepoScope

    assert RepoScope[scope].matches(is_awesome, is_python) is expected


def test_init_help_lists_all_components() -> None:
    """Verify the init command help text lists every registered component."""
    from repomatic.cli import init_project

    help_text = init_project.help
    assert help_text is not None
    for name in ALL_COMPONENTS:
        assert name in help_text, f"Component {name!r} missing from init help text"


def test_supported_config_types() -> None:
    """Verify that expected config types are registered as ToolConfigComponent."""
    for name in ("mypy", "ruff", "pytest", "bumpversion", "typos"):
        assert isinstance(_BY_NAME[name], ToolConfigComponent)


def test_config_type_has_required_fields() -> None:
    """Verify that each tool config component has all required fields."""
    for comp in COMPONENTS:
        if not isinstance(comp, ToolConfigComponent):
            continue
        assert comp.source_file
        assert comp.tool_section
        assert comp.description


@pytest.mark.parametrize(
    "config_type",
    sorted(c.name for c in COMPONENTS if isinstance(c, ToolConfigComponent)),
)
def test_returns_non_empty_string(config_type: str) -> None:
    """Verify that export_content returns a non-empty string."""
    comp = _BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)
    content = export_content(comp.source_file)
    assert isinstance(content, str)
    assert len(content) > 0


@pytest.mark.parametrize(
    "config_type",
    sorted(c.name for c in COMPONENTS if isinstance(c, ToolConfigComponent)),
)
def test_returns_valid_toml(config_type: str) -> None:
    """Verify that the returned content is valid TOML."""
    comp = _BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)
    content = export_content(comp.source_file)
    parsed = tomllib.loads(content)
    assert isinstance(parsed, dict)


@pytest.mark.parametrize(
    "config_type",
    sorted(c.name for c in COMPONENTS if isinstance(c, ToolConfigComponent)),
)
def test_native_format_no_tool_prefix(config_type: str) -> None:
    """Verify that native format does not have [tool.X] prefix."""
    comp = _BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)
    content = export_content(comp.source_file)
    parsed = tomllib.loads(content)
    # Native format should NOT have a "tool" key at the root.
    assert "tool" not in parsed


def test_unknown_file_raises_error() -> None:
    """Verify that an unknown file raises ValueError."""
    with pytest.raises(ValueError, match="Unknown file"):
        export_content("nonexistent.toml")


@pytest.mark.parametrize("filename", list(EXPORTABLE_FILES.keys()))
def test_exportable_file_loadable(filename: str) -> None:
    """Verify every registered data file can be loaded (no dangling symlinks)."""
    content = export_content(filename)
    assert len(content) > 0


# Keys intentionally different between template and repomatic's own pyproject.toml.
# - ``exclude``: key exists only in the template (for downstream), not in own config.
# - ``superset``: every template list entry must appear in own config, but own may
#   have extras.
_TEMPLATE_EXCLUDE_KEYS: dict[str, frozenset[str]] = {
    "bumpversion": frozenset({"current_version"}),
    "mypy": frozenset(),
    "pytest": frozenset({"addopts"}),
    "ruff": frozenset({"extend-include"}),
    "typos": frozenset(),
}
_TEMPLATE_SUPERSET_KEYS: dict[str, frozenset[str]] = {
    "bumpversion": frozenset({"files"}),
    "mypy": frozenset(),
    "pytest": frozenset(),
    "ruff": frozenset(),
    "typos": frozenset(),
}


_SCOPE_SPECIFIC_CONFIGS = frozenset({"lychee"})
"""ToolConfigComponents with non-ALL scope whose templates are intentionally
different from repomatic's own config (e.g., awesome-only lychee excludes
crawler-blocking sites, while repomatic's lychee excludes GitHub URLs)."""


@pytest.mark.parametrize(
    "config_type",
    sorted(
        c.name
        for c in COMPONENTS
        if isinstance(c, ToolConfigComponent) and c.name not in _SCOPE_SPECIFIC_CONFIGS
    ),
)
def test_template_matches_own_pyproject(config_type: str) -> None:
    """Verify bundled template stays in sync with repomatic's own config.

    Template keys (minus intentional exclusions) must match the corresponding
    ``[tool.*]`` section. List keys marked as superset require every template
    entry to appear in own config, but own config may have extras.

    Scope-specific configs (e.g., lychee for awesome repos) are excluded
    because their templates are intentionally different from repomatic's own.
    """
    comp = _BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)
    template = tomllib.loads(export_content(comp.source_file))

    project_root = Path(__file__).resolve().parent.parent
    tool_sections = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="UTF-8")
    ).get("tool", {})
    if config_type not in tool_sections:
        # Repo relies on the bundled default at runtime; no [tool.X] to compare.
        pytest.skip(f"No [tool.{config_type}] in pyproject.toml")
    own_config = tool_sections[config_type]

    exclude = _TEMPLATE_EXCLUDE_KEYS.get(config_type, frozenset())
    superset = _TEMPLATE_SUPERSET_KEYS.get(config_type, frozenset())

    def assert_subset(tmpl: dict, own: dict, path: str = "") -> None:
        for key, value in tmpl.items():
            full = f"{path}.{key}" if path else key
            if key in exclude:
                continue
            if key in superset:
                for entry in value:
                    assert entry in own[key], (
                        f"Template entry missing from [tool.{config_type}] "
                        f"{full}: {entry}"
                    )
                continue
            assert key in own, (
                f"Template key {full!r} missing from "
                f"[tool.{config_type}] in pyproject.toml"
            )
            if isinstance(value, dict):
                assert_subset(value, own[key], full)
            else:
                assert own[key] == value, (
                    f"[tool.{config_type}] {full!r}: "
                    f"expected {value!r}, got {own[key]!r}"
                )

    assert_subset(template, own_config)


def test_bundled_renovate_matches_processed_root() -> None:
    """Verify bundled renovate.json5 matches processed root file.

    The root ``renovate.json5`` is the source of truth. The bundled version
    in ``repomatic/data/`` should match the root file with repo-specific
    settings (``assignees`` and the self-referencing uv ``customManagers``
    entry) removed.

    If this test fails, regenerate the bundled file by running:
        uv run repomatic init renovate --output-dir repomatic/data
    """
    root_path = Path(__file__).parent.parent / "renovate.json5"
    assert root_path.exists(), "Root renovate.json5 not found"

    content = root_path.read_text(encoding="UTF-8")
    processed = _strip_renovate_repo_settings(content)

    bundled_content = get_data_content("renovate.json5")

    assert bundled_content.strip() == processed.strip(), (
        "Bundled renovate.json5 is out of sync with root file.\n"
        "Regenerate with: uv run repomatic init renovate"
        " --output-dir repomatic/data"
    )


# --- Ruff config tests ---


def test_has_preview_enabled() -> None:
    """Verify that preview mode is enabled."""
    content = export_content("ruff.toml")
    parsed = tomllib.loads(content)
    assert parsed.get("preview") is True


def test_has_fix_settings() -> None:
    """Verify that fix settings are configured."""
    content = export_content("ruff.toml")
    parsed = tomllib.loads(content)

    assert parsed.get("fix") is True
    assert parsed.get("unsafe-fixes") is True
    assert parsed.get("show-fixes") is True


def test_has_lint_section() -> None:
    """Verify that the lint section exists with expected settings."""
    content = export_content("ruff.toml")
    parsed = tomllib.loads(content)

    assert "lint" in parsed
    lint = parsed["lint"]
    assert lint.get("future-annotations") is True
    assert "ignore" in lint
    assert isinstance(lint["ignore"], list)


@pytest.mark.parametrize("expected_ignore", ["D400", "ERA001"])
def test_has_expected_ignore_rules(expected_ignore: str) -> None:
    """Verify that expected rules are in the ignore list."""
    content = export_content("ruff.toml")
    parsed = tomllib.loads(content)
    ignore = parsed["lint"]["ignore"]
    assert expected_ignore in ignore


def test_has_format_section() -> None:
    """Verify that the format section exists with docstring formatting enabled."""
    content = export_content("ruff.toml")
    parsed = tomllib.loads(content)

    assert "format" in parsed
    assert parsed["format"].get("docstring-code-format") is True


# --- Mypy config tests ---


@pytest.mark.parametrize(
    "setting",
    [
        "warn_unused_configs",
        "warn_redundant_casts",
        "warn_unused_ignores",
        "warn_return_any",
        "warn_unreachable",
        "pretty",
    ],
)
def test_has_expected_settings(setting: str) -> None:
    """Verify that expected settings are present."""
    content = export_content("mypy.toml")
    parsed = tomllib.loads(content)
    assert setting in parsed
    assert parsed[setting] is True


# --- Pytest config tests ---


def test_has_addopts() -> None:
    """Verify that addopts list is present."""
    content = export_content("pytest.toml")
    parsed = tomllib.loads(content)

    assert "addopts" in parsed
    assert isinstance(parsed["addopts"], list)
    assert len(parsed["addopts"]) > 0


@pytest.mark.parametrize(
    "expected_opt",
    [
        "--durations=10",
        "--cov-branch",
        "--cov-report=term",
        "--cov-report=xml",
        "--junitxml=junit.xml",
    ],
)
def test_has_expected_addopts(expected_opt: str) -> None:
    """Verify that expected options are in addopts."""
    content = export_content("pytest.toml")
    parsed = tomllib.loads(content)
    addopts = parsed["addopts"]
    assert expected_opt in addopts


def test_has_xfail_strict() -> None:
    """Verify that xfail_strict is enabled."""
    content = export_content("pytest.toml")
    parsed = tomllib.loads(content)
    assert parsed.get("xfail_strict") is True


# --- Bumpversion config tests ---


def test_has_required_settings() -> None:
    """Verify that the configuration has required bumpversion settings."""
    content = export_content("bumpversion.toml")
    parsed = tomllib.loads(content)

    assert "current_version" in parsed
    assert "allow_dirty" in parsed
    assert "ignore_missing_files" in parsed


def test_has_files_section() -> None:
    """Verify that the configuration has file patterns defined."""
    content = export_content("bumpversion.toml")
    parsed = tomllib.loads(content)

    assert "files" in parsed
    assert isinstance(parsed["files"], list)
    assert len(parsed["files"]) > 0


# --- pyproject.toml merging tests ---


def test_init_config_adds_root_section() -> None:
    """Verify that init_config produces a [tool.mypy] section."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')
        f.flush()
        result = init_config("mypy", Path(f.name))
    Path(f.name).unlink()
    assert result is not None
    assert "[tool.mypy]" in result


def test_init_config_transforms_subsections() -> None:
    """Verify that ruff lint config appears under [tool.ruff]."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')
        f.flush()
        result = init_config("ruff", Path(f.name))
    Path(f.name).unlink()
    assert result is not None
    # tomlkit preserves the template's native dotted-key style.
    assert "lint.ignore" in result


def test_init_config_transforms_array_sections() -> None:
    """Verify that array sections get the tool prefix."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')
        f.flush()
        result = init_config("bumpversion", Path(f.name))
    Path(f.name).unlink()
    assert result is not None
    assert "[[tool.bumpversion.files]]" in result


def test_init_config_preserves_template_comments() -> None:
    """Verify that template comments are preserved during init."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')
        f.flush()
        result = init_config("bumpversion", Path(f.name))
    Path(f.name).unlink()
    assert result is not None
    # The bumpversion template has inline comments explaining config values.
    assert "# Update version in [project] section." in result


def test_init_config_lychee_preserves_other_sections() -> None:
    """Lychee init merges [tool.lychee] without stripping unrelated sections."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(
            "[tool.gitleaks]\n"
            "[tool.gitleaks.allowlist]\n"
            'description = "false positives"\n'
            'commits = ["abc123"]\n'
        )
        f.flush()
        result = init_config("lychee", Path(f.name))
    Path(f.name).unlink()
    assert result is not None
    parsed = tomllib.loads(result)
    # Lychee was added.
    assert "lychee" in parsed["tool"]
    assert "exclude" in parsed["tool"]["lychee"]
    # Gitleaks was preserved.
    assert "gitleaks" in parsed["tool"]
    assert parsed["tool"]["gitleaks"]["allowlist"]["commits"] == ["abc123"]


def test_full_ruff_init() -> None:
    """Verify full ruff config initialization."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')
        f.flush()
        result = init_config("ruff", Path(f.name))
    Path(f.name).unlink()
    assert result is not None
    parsed = tomllib.loads(result)

    assert "tool" in parsed
    assert "ruff" in parsed["tool"]
    assert parsed["tool"]["ruff"].get("preview") is True
    assert "lint" in parsed["tool"]["ruff"]
    assert "format" in parsed["tool"]["ruff"]


def test_adds_config_to_empty_pyproject() -> None:
    """Verify that config is added to a pyproject.toml without the section."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')
        f.flush()
        path = Path(f.name)

    try:
        result = init_config("ruff", path)
        assert result is not None
        assert "[tool.ruff]" in result
        assert "preview = true" in result
        # Verify dotted keys are preserved (ruff.toml uses dotted keys, not
        # sections).
        assert "lint.ignore" in result
        assert "format.docstring-code-format" in result
    finally:
        path.unlink()


def test_adds_bumpversion_with_array_sections() -> None:
    """Verify that bumpversion [[files]] sections are transformed."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')
        f.flush()
        path = Path(f.name)

    try:
        result = init_config("bumpversion", path)
        assert result is not None
        assert "[tool.bumpversion]" in result
        assert "[[tool.bumpversion.files]]" in result
    finally:
        path.unlink()


def test_returns_none_if_section_exists() -> None:
    """Verify that None is returned if the section already exists."""
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(
            '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.ruff]\npreview = false\n'
        )
        f.flush()
        path = Path(f.name)

    try:
        result = init_config("ruff", path)
        assert result is None
    finally:
        path.unlink()


def test_preserves_existing_content() -> None:
    """Verify that existing content is preserved when adding config."""
    original = '[project]\nname = "test"\nversion = "1.0.0"\n'
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(original)
        f.flush()
        path = Path(f.name)

    try:
        result = init_config("mypy", path)
        assert result is not None
        assert 'name = "test"' in result
        assert 'version = "1.0.0"' in result
        assert "[tool.mypy]" in result
    finally:
        path.unlink()


def test_unknown_config_type_raises_error() -> None:
    """Verify that an unknown config type raises TypeError."""
    with pytest.raises(TypeError, match="Unknown config type"):
        init_config("nonexistent", Path("pyproject.toml"))


# --- Init orchestration tests ---


def test_default_version_pin():
    """Verify version pin is derived correctly."""
    pin = default_version_pin()
    assert pin.startswith("v")
    # Should not contain .dev suffix.
    assert ".dev" not in pin


def test_init_default_components():
    """Verify default selection includes expected components."""
    from repomatic.registry import InitDefault

    defaults = {
        c.name
        for c in COMPONENTS
        if c.init_default in (InitDefault.INCLUDE, InitDefault.EXCLUDE)
    }
    assert "changelog" in defaults
    assert "labels" in defaults
    assert "renovate" in defaults
    assert "skills" in defaults
    assert "workflows" in defaults
    # Tool configs should not be in defaults.
    assert "ruff" not in defaults
    assert "bumpversion" not in defaults


def test_init_creates_all_default_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify all default component files are created (with no exclusions)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["agents", "labels", "renovate", "skills"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    # All components: agents, changelog, labels, renovate, skills, workflows.
    # Opt-in workflows are excluded by default. Awesome-only skills are
    # included because ``include = ["skills"]`` bypasses scope filtering.
    config_file_count = sum(
        len(c.files) for c in COMPONENTS if isinstance(c, BundledComponent)
    )
    opt_in_count = sum(1 for f in _BY_NAME["workflows"].files if f.config_key)
    default_workflows = len(REUSABLE_WORKFLOWS) - opt_in_count
    expected_count = default_workflows + config_file_count + 1
    assert len(result.created) == expected_count
    assert len(result.skipped) == 0
    assert len(result.warnings) == 0

    # Verify workflow files exist (excluding opt-in workflows).
    for filename in REUSABLE_WORKFLOWS:
        if filename not in _OPT_IN_IDS:
            assert (tmp_path / ".github" / "workflows" / filename).exists()

    # Verify config files exist.
    assert (tmp_path / "labels.toml").exists()
    assert (tmp_path / ".github" / "labeller-file-based.yaml").exists()
    assert (tmp_path / ".github" / "labeller-content-based.yaml").exists()

    # Verify changelog exists.
    assert (tmp_path / "changelog.md").exists()


def test_init_creates_changelog(tmp_path: Path):
    """Verify changelog is created with expected content."""
    result = run_init(output_dir=tmp_path, components=("changelog",))

    changelog = tmp_path / "changelog.md"
    assert changelog.exists()
    content = changelog.read_text(encoding="UTF-8")
    assert content.startswith("# Changelog")
    assert "## [Unreleased]" in content
    assert "changelog.md" in result.created


def test_init_creates_parent_dirs(tmp_path: Path):
    """Verify .github/workflows/ is created automatically."""
    assert not (tmp_path / ".github").exists()
    run_init(output_dir=tmp_path, components=("workflows",))
    assert (tmp_path / ".github" / "workflows").is_dir()


def test_init_workflow_paths_config_flows_to_generated_files(tmp_path: Path):
    """`[tool.repomatic.workflow]` paths knobs reach the generated workflows.

    End-to-end check that a `Config` carrying `extra_paths`, `ignore_paths`,
    and per-workflow `paths` overrides is honored by `_init_workflows` when
    rendering thin callers.
    """
    from repomatic.config import WorkflowConfig

    config = Config(
        workflow=WorkflowConfig(
            extra_paths=["repo-specific.sh"],
            ignore_paths=["uv.lock"],
            paths={"renovate.yaml": ["only-this.json5"]},
        ),
    )
    run_init(
        output_dir=tmp_path,
        components=("workflows",),
        config=config,
    )

    # Per-workflow override replaces the renovate.yaml caller's paths.
    renovate = (tmp_path / ".github" / "workflows" / "renovate.yaml").read_text(
        encoding="UTF-8",
    )
    assert "only-this.json5" in renovate
    # Override skips global knobs: extras don't leak to overridden workflows.
    assert "repo-specific.sh" not in renovate

    # changelog.yaml has push.paths in canonical and is not overridden, so
    # global knobs apply.
    changelog = (tmp_path / ".github" / "workflows" / "changelog.yaml").read_text(
        encoding="UTF-8",
    )
    assert "repo-specific.sh" in changelog
    assert "uv.lock" not in changelog
    # Untouched canonical entries survive.
    assert "changelog.md" in changelog


def test_init_existing_changelog_skipped(tmp_path: Path):
    """Verify existing changelog is not overwritten."""
    changelog = tmp_path / "changelog.md"
    changelog.write_text("# My existing changelog\n", encoding="UTF-8")

    result = run_init(output_dir=tmp_path, components=("changelog",))

    assert "changelog.md" in result.skipped
    # Content should not be overwritten.
    assert changelog.read_text(encoding="UTF-8") == "# My existing changelog\n"


def test_init_idempotent(tmp_path: Path):
    """Verify a second run with no on-disk changes produces no updates.

    Re-running `repomatic init` against an unchanged tree must be a no-op:
    files identical to what `init` would write are not reported as updated.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "fruitbasket"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )

    result1 = run_init(output_dir=tmp_path)
    assert len(result1.created) > 0
    assert len(result1.updated) == 0
    assert len(result1.skipped) == 0

    result2 = run_init(output_dir=tmp_path)
    assert len(result2.created) == 0
    assert len(result2.updated) == 0
    # Only changelog is skipped (never overwritten).
    assert result2.skipped == ["changelog.md"]


@pytest.mark.parametrize(
    "component",
    [
        "workflows",
        "skills",
        "labels",
        "renovate",
    ],
)
def test_init_per_component_idempotent(tmp_path: Path, component: str):
    """Verify re-running a single-component init produces no spurious updates.

    Locks in the per-component no-op behavior: when bundled content matches
    what is already on disk, the second run must not report any file as
    `updated`. Regression guard for skills/workflows wrongly flagged as
    updated when their content was unchanged.
    """
    first = run_init(output_dir=tmp_path, components=(component,))
    assert len(first.created) > 0
    assert len(first.updated) == 0

    second = run_init(output_dir=tmp_path, components=(component,))
    assert second.created == []
    assert second.updated == []


def test_init_only_labels(tmp_path: Path):
    """Verify only label files are created."""
    result = run_init(output_dir=tmp_path, components=("labels",))

    created_set = set(result.created)
    assert "labels.toml" in created_set
    assert ".github/labeller-file-based.yaml" in created_set
    assert ".github/labeller-content-based.yaml" in created_set

    # No workflows or changelog should be created.
    for filename in REUSABLE_WORKFLOWS:
        assert f".github/workflows/{filename}" not in created_set
    assert "changelog.md" not in created_set


def test_init_only_skills(tmp_path: Path):
    """Verify only skill files are created.

    Scope exclusions are bypassed when components are explicitly requested,
    so all 18 skills (including awesome-only ones) are created.
    """
    result = run_init(output_dir=tmp_path, components=("skills",))

    created_set = set(result.created)
    assert len(created_set) == 18

    # Verify all skill files are created, including awesome-only ones.
    for name in (
        "av-false-positive",
        "awesome-triage",
        "babysit-ci",
        "benchmark-update",
        "brand-assets",
        "file-bug-report",
        "repomatic-audit",
        "repomatic-changelog",
        "repomatic-deps",
        "repomatic-init",
        "repomatic-lint",
        "repomatic-release",
        "repomatic-sync",
        "repomatic-test",
        "repomatic-topics",
        "sphinx-docs-sync",
        "translation-sync",
        "upstream-audit",
    ):
        rel = f".claude/skills/{name}/SKILL.md"
        assert rel in created_set
        assert (tmp_path / ".claude" / "skills" / name / "SKILL.md").exists()

    # No workflows or changelog should be created.
    assert "changelog.md" not in created_set


def test_skills_consistency():
    """Verify all .claude/skills/ directories have matching entries in code.

    Guards against adding a skill definition to ``.claude/skills/`` without
    registering it in the component registry, ``SKILL_PHASES``, and the
    ``repomatic/data/`` symlinks.
    """
    # Collect skill directories from the filesystem.
    skills_dir = Path(__file__).resolve().parents[1] / ".claude" / "skills"
    fs_skills = {p.parent.name for p in skills_dir.glob("*/SKILL.md")}

    # Collect skills registered in the component registry.
    component_skills = {entry.file_id for entry in _BY_NAME["skills"].files}

    # Collect skills registered in SKILL_PHASES.
    phase_skills = set(SKILL_PHASES)

    # Collect data symlinks.
    data_dir = Path(__file__).resolve().parents[1] / "repomatic" / "data"
    data_skills = {p.stem.removeprefix("skill-") for p in data_dir.glob("skill-*.md")}

    assert fs_skills == component_skills, (
        f"Registry mismatch: "
        f"missing={fs_skills - component_skills}, "
        f"extra={component_skills - fs_skills}"
    )
    assert fs_skills == data_skills, (
        f"Data symlinks mismatch: "
        f"missing={fs_skills - data_skills}, "
        f"extra={data_skills - fs_skills}"
    )
    assert fs_skills == phase_skills, (
        f"SKILL_PHASES mismatch: "
        f"missing={fs_skills - phase_skills}, "
        f"stale={phase_skills - fs_skills}"
    )


def test_init_only_renovate(tmp_path: Path):
    """Verify only renovate config is created."""
    result = run_init(output_dir=tmp_path, components=("renovate",))

    assert "renovate.json5" in set(result.created)
    assert (tmp_path / "renovate.json5").exists()
    # No other default components.
    assert "changelog.md" not in set(result.created)


def test_init_only_workflows(tmp_path: Path):
    """Verify only workflow files are created."""
    result = run_init(output_dir=tmp_path, components=("workflows",))

    created_set = set(result.created)
    # Opt-in workflows are excluded by default.
    for filename in REUSABLE_WORKFLOWS:
        if filename in _OPT_IN_IDS:
            assert f".github/workflows/{filename}" not in created_set
        else:
            assert f".github/workflows/{filename}" in created_set

    # No config files or changelog.
    assert "renovate.json5" not in created_set
    assert "changelog.md" not in created_set


def test_init_always_overwrites_managed_files(tmp_path: Path):
    """Verify managed files are always replaced on re-run."""
    # Create a workflow file with old content.
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    target = workflows_dir / "lint.yaml"
    target.write_text("# Old content\n", encoding="UTF-8")

    result = run_init(
        output_dir=tmp_path,
        components=("workflows",),
    )

    assert ".github/workflows/lint.yaml" in result.updated
    assert len(result.skipped) == 0
    # Content should be replaced.
    content = target.read_text(encoding="UTF-8")
    assert content != "# Old content\n"


def test_init_changelog_never_overwritten(tmp_path: Path):
    """Verify an existing changelog.md is never overwritten."""
    changelog = tmp_path / "changelog.md"
    changelog.write_text("# Old content\n", encoding="UTF-8")

    result = run_init(
        output_dir=tmp_path,
        components=("changelog",),
    )

    assert "changelog.md" in result.skipped
    assert len(result.created) == 0
    assert len(result.updated) == 0
    # Content should be preserved.
    content = changelog.read_text(encoding="UTF-8")
    assert content == "# Old content\n"


def test_init_tool_configs_no_pyproject(tmp_path: Path):
    """Verify warning when pyproject.toml is missing."""
    result = run_init(
        output_dir=tmp_path,
        components=("ruff",),
    )

    assert len(result.warnings) == 1
    assert "pyproject.toml not found" in result.warnings[0]


def test_init_version_pinned(tmp_path: Path):
    """Verify generated workflows contain the specified version pin."""
    run_init(
        output_dir=tmp_path,
        components=("workflows",),
        version="v5.9.1",
    )

    # Check that generated workflow files contain the version pin.
    # Opt-in workflows are excluded by default.
    for filename in REUSABLE_WORKFLOWS:
        if filename in _OPT_IN_IDS:
            continue
        wf_path = tmp_path / ".github" / "workflows" / filename
        content = wf_path.read_text(encoding="UTF-8")
        assert "@v5.9.1" in content


def test_init_with_specific_tool_configs(tmp_path: Path):
    """Verify multiple tool configs are merged into pyproject.toml."""
    # Create a minimal pyproject.toml.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test-project"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )

    result = run_init(
        output_dir=tmp_path,
        components=("ruff", "bumpversion"),
    )

    assert len(result.warnings) == 0

    # Verify requested tool sections were merged.
    content = pyproject.read_text(encoding="UTF-8")
    assert "[tool.ruff]" in content
    assert "[tool.bumpversion]" in content


def test_init_with_single_tool_config(tmp_path: Path):
    """Verify only requested tool config is merged."""
    # Create a minimal pyproject.toml.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test-project"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )

    run_init(
        output_dir=tmp_path,
        components=("ruff",),
    )

    # Only ruff should be merged, not bumpversion.
    content = pyproject.read_text(encoding="UTF-8")
    assert "[tool.ruff]" in content
    assert "[tool.bumpversion]" not in content


# --- Bumpversion config update tests ---


# Minimal pyproject with existing bumpversion config (no dev versioning).
PYPROJECT_WITH_BUMPVERSION = """\
[project]
name = "test-project"
version = "7.5.3.dev0"

[tool.bumpversion]
current_version = "7.5.3.dev0"
allow_dirty = true
parse = "(?P<major>\\\\d+)\\\\.(?P<minor>\\\\d+)\\\\.(?P<patch>\\\\d+)(\\\\.dev(?P<dev>\\\\d+))?"
serialize = [
  "{major}.{minor}.{patch}.dev{dev}",
  "{major}.{minor}.{patch}",
]

[[tool.bumpversion.files]]
filename = "./pyproject.toml"
search = 'version = "{current_version}"'
replace = 'version = "{new_version}"'

[[tool.bumpversion.files]]
filename = "./changelog.md"
search = "## [{current_version} (unreleased)]("
replace = "## [{new_version} (unreleased)]("
"""


# Mirrors the motivating downstream case (kdeldycke/dotfiles): a non-Python repo
# that carries its own `[tool.typos]` (project-specific excludes and a couple of
# local identifiers/words) but never received the bundled canonical identifiers.
# The local entries use domain-neutral placeholders `typos` does not flag, so
# the `fix-typos` job cannot rewrite this fixture.
PYPROJECT_WITH_TYPOS = """\
[tool.typos]
files.extend-exclude = ["assets/Monokai Soda.terminal"]

[tool.typos.default.extend-identifiers]
getForecastForCity = "getForecastForCity"

[tool.typos.default.extend-words]
monsoon = "monsoon"
"""


def _canonical_typos_identifiers() -> dict[str, str]:
    """Canonical proper-noun identifiers bundled in `repomatic/data/typos.toml`.

    Loaded from the template so tests can assert on the canonical map of
    misspelled keys to corrected values without writing those misspelled keys as
    literals here, which the `fix-typos` job would otherwise "correct".
    """
    parsed = tomllib.loads(get_data_content("typos.toml"))
    identifiers = parsed["default"]["extend-identifiers"]
    return {str(k): str(v) for k, v in identifiers.items()}


def _make_pyproject_with_template_bumpversion(version: str = "7.5.3.dev0") -> str:
    """Generate a pyproject.toml with the bumpversion section from the template.

    Used as a fixture for tests that need an already-up-to-date config.
    """
    base = f'[project]\nname = "test-project"\nversion = "{version}"\n'
    with NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(base)
        f.flush()
        result = init_config("bumpversion", Path(f.name))
    Path(f.name).unlink()
    assert result is not None
    return result.replace(
        'current_version = "0.0.0.dev0"',
        f'current_version = "{version}"',
    )


def test_syncs_bumpversion_template_keys(tmp_path: Path) -> None:
    """Verify template keys are synced into existing bumpversion config."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_BUMPVERSION, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    assert "ignore_missing_files = true" in result
    assert "parts.dev.values = " in result


def test_replaces_bumpversion_files_from_template(tmp_path: Path) -> None:
    """Verify [[tool.bumpversion.files]] entries come from the template."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_BUMPVERSION, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    assert "[[tool.bumpversion.files]]" in result
    # Template entries are present.
    assert 'filename = "./pyproject.toml"' in result
    assert 'filename = "./changelog.md"' in result
    assert 'filename = "./citation.cff"' in result
    assert 'glob = "./**/__init__.py"' in result


def test_preserves_other_pyproject_sections(tmp_path: Path) -> None:
    """Verify [project] and other sections are unchanged."""
    content = (
        '[project]\nname = "test-project"\nversion = "2.0.0.dev0"\n\n'
        "[tool.ruff]\npreview = true\n\n"
        "[tool.bumpversion]\n"
        'current_version = "2.0.0.dev0"\n'
        "allow_dirty = true\n"
        'parse = "(?P<major>\\\\d+)"\n\n'
        "[[tool.bumpversion.files]]\n"
        'filename = "./pyproject.toml"\n'
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    assert 'name = "test-project"' in result
    assert "[tool.ruff]" in result
    assert "preview = true" in result


def test_skips_already_up_to_date(tmp_path: Path) -> None:
    """Verify config matching the template returns None (no changes needed)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_make_pyproject_with_template_bumpversion(), encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is None


def test_replaces_old_changelog_pattern(tmp_path: Path) -> None:
    """Verify old changelog pattern without backticks is replaced by template."""
    content = (
        '[project]\nname = "test"\nversion = "1.0.0.dev0"\n\n'
        "[tool.bumpversion]\n"
        'current_version = "1.0.0.dev0"\n'
        "allow_dirty = true\n"
        "parse = '(?P<major>\\d+)'\n\n"
        "[[tool.bumpversion.files]]\n"
        'filename = "./changelog.md"\n'
        'search = "## [{current_version} (unreleased)]("\n'
        'replace = "## [{new_version} (unreleased)]("\n'
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    # Template has backtick-escaped pattern.
    assert "## [`{current_version}` (unreleased)](" in result
    assert "## [`{new_version}` (unreleased)](" in result


def test_bumpversion_update_idempotent(tmp_path: Path) -> None:
    """Verify running update twice produces the same result."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_BUMPVERSION, encoding="UTF-8")

    # First run: should update.
    result1 = init_config("bumpversion", pyproject)
    assert result1 is not None
    pyproject.write_text(result1, encoding="UTF-8")

    # Second run: should be a no-op.
    result2 = init_config("bumpversion", pyproject)
    assert result2 is None


def test_bumpversion_update_valid_toml(tmp_path: Path) -> None:
    """Verify updated pyproject.toml is still valid TOML."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_BUMPVERSION, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    parsed = tomllib.loads(result)
    bv = parsed["tool"]["bumpversion"]
    assert "parse" in bv
    assert "serialize" in bv
    assert "parts" in bv
    assert "dev" in bv["parts"]


def test_preserves_local_array_entries(tmp_path: Path) -> None:
    """Verify local [[tool.bumpversion.files]] entries survive ongoing sync."""
    content = (
        '[project]\nname = "test"\nversion = "1.0.0.dev0"\n\n'
        "[tool.bumpversion]\n"
        'current_version = "1.0.0.dev0"\n'
        "allow_dirty = true\n"
        'parse = "(?P<major>\\\\d+)"\n'
        'serialize = ["{major}"]\n\n'
        "[[tool.bumpversion.files]]\n"
        'filename = "./pyproject.toml"\n'
        "search = 'version = \"{current_version}\"'\n"
        "replace = 'version = \"{new_version}\"'\n\n"
        "[[tool.bumpversion.files]]\n"
        'filename = "./readme.md"\n'
        "ignore_missing_version = true\n"
        'search = "raw.githubusercontent.com/test/main/"\n'
        'replace = "raw.githubusercontent.com/test/v{new_version}/"\n'
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    bv = _BY_NAME["bumpversion"]
    assert isinstance(bv, ToolConfigComponent)
    result = _update_tool_config(content, bv, pyproject)

    assert result is not None
    parsed = tomllib.loads(result)
    files_entries = parsed["tool"]["bumpversion"]["files"]
    # Local entry targeting readme.md must survive.
    readme_entries = [e for e in files_entries if e.get("filename") == "./readme.md"]
    assert len(readme_entries) == 1
    assert readme_entries[0]["search"] == "raw.githubusercontent.com/test/main/"
    assert readme_entries[0]["ignore_missing_version"] is True


def test_ongoing_sync_idempotent_with_local_entries(tmp_path: Path) -> None:
    """Verify ongoing sync with local entries is idempotent after first run."""
    content = (
        '[project]\nname = "test"\nversion = "1.0.0.dev0"\n\n'
        "[tool.bumpversion]\n"
        'current_version = "1.0.0.dev0"\n'
        "allow_dirty = true\n"
        'parse = "(?P<major>\\\\d+)"\n'
        'serialize = ["{major}"]\n\n'
        "[[tool.bumpversion.files]]\n"
        'filename = "./readme.md"\n'
        "ignore_missing_version = true\n"
        'search = "example.com/main/"\n'
        'replace = "example.com/v{new_version}/"\n'
    )
    bv_comp = _BY_NAME["bumpversion"]
    assert isinstance(bv_comp, ToolConfigComponent)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    # First run: updates template entries.
    result1 = _update_tool_config(content, bv_comp, pyproject)
    assert result1 is not None
    pyproject.write_text(result1, encoding="UTF-8")

    # Second run: should be a no-op.
    result2 = _update_tool_config(result1, bv_comp, pyproject)
    assert result2 is None


def test_ongoing_sync_no_duplicate_template_entries(tmp_path: Path) -> None:
    """Verify template entries are not duplicated when also present locally."""
    content = PYPROJECT_WITH_BUMPVERSION
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    parsed = tomllib.loads(result)
    files_entries = parsed["tool"]["bumpversion"]["files"]
    # Template entries for pyproject.toml should appear exactly twice (version + tag).
    pyproject_entries = [
        e for e in files_entries if e.get("filename") == "./pyproject.toml"
    ]
    assert len(pyproject_entries) == 2


def test_local_entries_preserved_via_update(tmp_path: Path) -> None:
    """Verify local array entries survive ongoing sync via dict comparison."""
    content = (
        '[project]\nname = "test"\nversion = "1.0.0.dev0"\n\n'
        "[tool.bumpversion]\n"
        'current_version = "1.0.0.dev0"\n'
        "allow_dirty = true\n"
        'parse = "(?P<major>\\\\d+)"\n'
        'serialize = ["{major}"]\n\n'
        "[[tool.bumpversion.files]]\n"
        'filename = "./pyproject.toml"\n'
        "search = 'version = \"{current_version}\"'\n"
        "replace = 'version = \"{new_version}\"'\n\n"
        "[[tool.bumpversion.files]]\n"
        'filename = "./custom.txt"\n'
        'search = "x"\n'
        'replace = "y"\n'
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    bv = _BY_NAME["bumpversion"]
    assert isinstance(bv, ToolConfigComponent)
    result = _update_tool_config(content, bv, pyproject)

    assert result is not None
    parsed = tomllib.loads(result)
    files = parsed["tool"]["bumpversion"]["files"]
    custom = [e for e in files if e.get("filename") == "./custom.txt"]
    assert len(custom) == 1
    assert custom[0]["search"] == "x"


def test_local_entry_comments_preserved(tmp_path: Path) -> None:
    """Verify comments between local [[files]] entries survive ongoing sync.

    Regression test for the sync-bumpversion job stripping comments from
    downstream repos (e.g., click-extra#1595).

    .. note::
        tomlkit stores comments preceding the *first* local entry in the
        parent table body, not in the AoT. These are lost during section
        replacement. Comments *between* local entries survive because tomlkit
        attaches them to the preceding entry's trivia.
    """
    content = (
        '[project]\nname = "test"\nversion = "1.0.0.dev0"\n\n'
        "[tool.bumpversion]\n"
        'current_version = "1.0.0.dev0"\n'
        "allow_dirty = true\n"
        'parse = "(?P<major>\\\\d+)"\n'
        'serialize = ["{major}"]\n\n'
        "[[tool.bumpversion.files]]\n"
        'filename = "./pyproject.toml"\n'
        "search = 'version = \"{current_version}\"'\n"
        "replace = 'version = \"{new_version}\"'\n\n"
        "# Pin image URLs from main to the release tag.\n"
        "[[tool.bumpversion.files]]\n"
        'filename = "./readme.md"\n'
        "ignore_missing_version = true\n"
        'search = "raw.githubusercontent.com/test/main/"\n'
        'replace = "raw.githubusercontent.com/test/v{new_version}/"\n\n'
        "# Restore image URLs from the previous release tag back to main.\n"
        "[[tool.bumpversion.files]]\n"
        'filename = "./readme.md"\n'
        "ignore_missing_version = true\n"
        'search = "raw.githubusercontent.com/test/v{current_version}/"\n'
        'replace = "raw.githubusercontent.com/test/main/"\n'
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    bv = _BY_NAME["bumpversion"]
    assert isinstance(bv, ToolConfigComponent)
    result = _update_tool_config(content, bv, pyproject)

    assert result is not None
    # Comments between local entries survive (attached to preceding entry's
    # trivia by tomlkit).
    assert "# Restore image URLs from the previous release tag back to main." in result
    # The local entries themselves must survive.
    assert 'search = "raw.githubusercontent.com/test/main/"' in result
    assert 'search = "raw.githubusercontent.com/test/v{current_version}/"' in result


def test_syncs_typos_identifiers_into_existing_section(tmp_path: Path) -> None:
    """Canonical identifiers merge into a pre-existing typos section.

    Regression test for the BOOTSTRAP footgun: a downstream repo carrying its
    own `[tool.typos]` never received the bundled proper-noun identifiers,
    because a BOOTSTRAP insert skipped the existing section and typos reads
    `[tool.typos]` natively (no bundled fallback at runtime).
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_TYPOS, encoding="UTF-8")

    result = init_config("typos", pyproject)

    assert result is not None
    identifiers = tomllib.loads(result)["tool"]["typos"]["default"][
        "extend-identifiers"
    ]
    # Compared against the bundled template so the misspelled canonical keys
    # never appear as literals here, where `fix-typos` would "correct" them.
    assert _canonical_typos_identifiers().items() <= identifiers.items()


def test_typos_preserves_local_inline_table_keys(tmp_path: Path) -> None:
    """Local keys inside a shared inline table survive alongside canonical ones."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_TYPOS, encoding="UTF-8")

    result = init_config("typos", pyproject)

    assert result is not None
    default = tomllib.loads(result)["tool"]["typos"]["default"]
    # Local additions kept.
    assert default["extend-identifiers"]["getForecastForCity"] == "getForecastForCity"
    assert default["extend-words"]["monsoon"] == "monsoon"
    # Template entries present too.
    assert default["extend-words"]["astroid"] == "astroid"
    assert default["extend-ignore-re"]


def test_typos_preserves_local_only_table(tmp_path: Path) -> None:
    """A table the template omits (`files`) survives the sync untouched."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_TYPOS, encoding="UTF-8")

    result = init_config("typos", pyproject)

    assert result is not None
    files = tomllib.loads(result)["tool"]["typos"]["files"]
    assert files["extend-exclude"] == ["assets/Monokai Soda.terminal"]


def test_typos_canonical_value_wins_on_conflict(tmp_path: Path) -> None:
    """Canonical capitalization overrides a wrong local target on shared keys."""
    # Seed a canonical identifier with a wrong local target (the misspelled key
    # mapped to itself), derived from the template so no misspelled literal
    # appears in this file, then assert the canonical capitalization wins.
    key, canonical = next(
        (k, v) for k, v in _canonical_typos_identifiers().items() if k != v
    )
    content = f'[tool.typos.default.extend-identifiers]\n{key} = "{key}"\n'
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    result = init_config("typos", pyproject)

    assert result is not None
    identifiers = tomllib.loads(result)["tool"]["typos"]["default"][
        "extend-identifiers"
    ]
    assert identifiers[key] == canonical


def test_typos_update_idempotent(tmp_path: Path) -> None:
    """The typos sync is a no-op on the second run."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_TYPOS, encoding="UTF-8")

    first = init_config("typos", pyproject)
    assert first is not None
    pyproject.write_text(first, encoding="UTF-8")

    assert init_config("typos", pyproject) is None


def test_typos_merged_inline_tables_use_pyproject_fmt_spacing(tmp_path: Path) -> None:
    """Merged inline tables render `{ ... }`, matching pyproject-fmt.

    A compact `{...}` would be re-spaced by the `format-pyproject` autofix job,
    producing churn on every run.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_TYPOS, encoding="UTF-8")

    result = init_config("typos", pyproject)

    assert result is not None
    identifiers_line = next(
        line
        for line in result.splitlines()
        if line.startswith("default.extend-identifiers")
    )
    assert "= { " in identifiers_line
    assert identifiers_line.endswith(" }")


# --- Init exclusion tests ---


def test_init_default_excludes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify default exclude skips agents, labels, renovate, and skills."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Agents, labels, skills, and renovate are excluded by default.
    assert "labels.toml" not in created_set
    assert "renovate.json5" not in created_set
    for _, rel_path in ((e.source, e.target) for e in _BY_NAME["agents"].files):
        assert rel_path not in created_set
    for _, rel_path in ((e.source, e.target) for e in _BY_NAME["skills"].files):
        assert rel_path not in created_set

    # Other default components should still be created.
    assert "changelog.md" in created_set

    assert result.excluded == ["agents", "labels", "renovate", "skills"]


def test_init_respects_exclude_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify exclude config skips listed components."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'exclude = ["skills", "labels"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Labels and skill files should not be created.
    assert "labels.toml" not in created_set
    for _, rel_path in ((e.source, e.target) for e in _BY_NAME["skills"].files):
        assert rel_path not in created_set

    # Renovate is also excluded by default.
    assert "renovate.json5" not in created_set

    # Other default components should still be created.
    assert "changelog.md" in created_set

    assert result.excluded == ["agents", "labels", "renovate", "skills"]


def test_init_respects_exclude_workflow_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify exclude config with workflow file entries skips those workflows."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'exclude = ["workflows/debug.yaml", "workflows/docs.yaml"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Excluded workflows should not be created.
    assert ".github/workflows/debug.yaml" not in created_set
    assert ".github/workflows/docs.yaml" not in created_set

    # Other workflows should still be created.
    assert ".github/workflows/lint.yaml" in created_set
    assert ".github/workflows/release.yaml" in created_set


def test_init_respects_exclude_skill_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify exclude config with skill file entries skips those skills."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'exclude = ["skills/repomatic-audit", "skills/repomatic-topics"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Excluded skills should not be created.
    assert ".claude/skills/repomatic-audit/SKILL.md" not in created_set
    assert ".claude/skills/repomatic-topics/SKILL.md" not in created_set

    # Other skills should still be created.
    assert ".claude/skills/repomatic-init/SKILL.md" in created_set


def test_init_changelog_excluded_for_awesome_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify changelog.md is not created for awesome-* repos."""
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-python")

    assert not (tmp_path / "changelog.md").exists()
    assert "changelog.md" not in result.created


def test_init_changelog_excluded_existing_for_awesome_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify existing changelog.md is flagged as excluded for awesome-* repos."""
    changelog = tmp_path / "changelog.md"
    changelog.write_text("# Changelog\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-python")

    assert "changelog.md" in result.excluded_existing


def test_init_codecov_excluded_existing_for_awesome_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify existing .github/codecov.yaml is flagged as excluded for awesome repos."""
    codecov = tmp_path / ".github" / "codecov.yaml"
    codecov.parent.mkdir(parents=True, exist_ok=True)
    codecov.write_text("comment:\n  layout: reach\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-billing")

    assert ".github/codecov.yaml" in result.excluded_existing


@pytest.mark.parametrize(
    "repo_slug",
    [
        pytest.param("user/some-project", id="non-awesome"),
        pytest.param("user/awesome-python", id="awesome"),
    ],
)
def test_include_config_includes_all_skill_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: str
):
    """Config ``include = ["skills"]`` produces all skills regardless of repo type."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["skills"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug=repo_slug)

    created_set = set(result.created)
    assert ".claude/skills/awesome-triage/SKILL.md" in created_set
    assert ".claude/skills/translation-sync/SKILL.md" in created_set
    assert ".claude/skills/repomatic-init/SKILL.md" in created_set


def test_explicit_component_bypasses_scope_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Explicit component request overrides scope exclusion.

    ``repomatic init codecov`` in an awesome repo should produce
    ``.github/codecov.yaml`` even though the codecov component has
    ``scope=PYTHON_ONLY`` and awesome repos are non-Python. Scope
    exclusions only apply during bare ``repomatic init`` (no explicit
    components).
    """
    monkeypatch.chdir(tmp_path)

    result = run_init(
        components=["codecov"],
        output_dir=tmp_path,
        repo_slug="user/awesome-billing",
    )

    created_set = set(result.created)
    assert ".github/codecov.yaml" in created_set


def test_include_config_bypasses_scope_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Config ``include`` bypasses scope exclusions, like CLI explicit naming.

    ``include = ["codecov"]`` in an awesome repo should create
    ``.github/codecov.yaml`` even though codecov is scoped to
    ``PYTHON_ONLY`` and awesome repos are non-Python.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.repomatic]\ninclude = ["codecov"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(
        output_dir=tmp_path,
        repo_slug="user/awesome-billing",
    )

    created_set = set(result.created)
    assert ".github/codecov.yaml" in created_set


def test_include_bypasses_scope_for_bundled_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Include bypasses scope for a ``BundledComponent`` with files.

    Codecov is ``PYTHON_ONLY`` and ``init_default=INCLUDE``. In a non-Python
    awesome repo it would normally be scope-excluded. With ``include``, the
    scope bypass falls through to file-level checks (the file has
    ``scope=ALL`` so nothing is excluded).
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.repomatic]\ninclude = ["codecov"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-billing")

    assert ".github/codecov.yaml" in set(result.created)


def test_include_bypasses_scope_for_tool_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Include bypasses scope for a ``ToolConfigComponent``.

    Lychee is ``AWESOME_ONLY``. In a non-awesome repo it would normally
    be scope-excluded. With ``include``, the scope bypass falls through
    to config-key and file-level checks, then the tool config is merged
    into ``pyproject.toml``.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["lychee"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/some-project")

    assert "pyproject.toml" in result.created
    content = pyproject.read_text(encoding="UTF-8")
    assert "[tool.lychee]" in content


def test_bare_init_applies_scope_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bare init without ``include`` or CLI components applies scope exclusions."""
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-billing")

    created_set = set(result.created)
    # PYTHON_ONLY component and workflow files are scope-excluded in awesome repos.
    assert ".github/codecov.yaml" not in created_set
    assert ".github/workflows/changelog.yaml" not in created_set
    assert ".github/workflows/debug.yaml" not in created_set
    assert ".github/workflows/release.yaml" not in created_set
    # Non-scoped workflows are still created.
    assert ".github/workflows/lint.yaml" in created_set


def test_file_level_include_bypasses_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """File-level ``include`` entry bypasses scope for that specific file only."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.repomatic]\ninclude = ["workflows/changelog.yaml"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-billing")

    created_set = set(result.created)
    # changelog.yaml (PYTHON_ONLY) included via file-level include.
    assert ".github/workflows/changelog.yaml" in created_set
    # Other PYTHON_ONLY workflows remain scope-excluded.
    assert ".github/workflows/debug.yaml" not in created_set
    assert ".github/workflows/release.yaml" not in created_set


def test_file_level_include_implies_parent_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """File-level ``include`` implicitly selects the parent component.

    ``include = ["skills/awesome-triage"]`` should select the skills
    component (which is default-excluded) and bypass scope for that
    specific file, without bypassing scope for other AWESOME_ONLY files.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills/awesome-triage"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/some-project")

    created_set = set(result.created)
    # awesome-triage created: parent component implicitly selected, scope bypassed.
    assert ".claude/skills/awesome-triage/SKILL.md" in created_set
    # Other non-scoped skills also created (component is fully selected).
    assert ".claude/skills/repomatic-init/SKILL.md" in created_set
    # translation-sync is AWESOME_ONLY and NOT in include: still scope-excluded.
    assert ".claude/skills/translation-sync/SKILL.md" not in created_set


def test_exclude_overrides_include_scope_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Explicit ``exclude`` entries take precedence over ``include`` scope bypass."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'exclude = ["skills/awesome-triage"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/some-project")

    created_set = set(result.created)
    # awesome-triage excluded by explicit exclude despite include scope bypass.
    assert ".claude/skills/awesome-triage/SKILL.md" not in created_set
    # Other awesome-only skills are included (scope bypassed by include).
    assert ".claude/skills/translation-sync/SKILL.md" in created_set
    assert ".claude/skills/repomatic-init/SKILL.md" in created_set


def test_publish_pypi_action_excluded_in_non_python_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`publish-pypi-action` is skipped when the repo is not a Python project.

    Reproduces the scenario where a non-Python repository (e.g., a dotfiles
    project) carries a `pyproject.toml` solely for `[tool.*]` configuration.
    The composite action targets PyPI publishing, so it has no purpose there.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.repomatic]\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/dotfiles")

    created_set = set(result.created)
    assert ".github/actions/publish-pypi/action.yaml" not in created_set


def test_publish_pypi_action_included_in_python_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`publish-pypi-action` is installed when the repo is a Python project."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "fruitbasket"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/fruitbasket")

    created_set = set(result.created)
    assert ".github/actions/publish-pypi/action.yaml" in created_set


def test_explicit_component_bypasses_python_only_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Explicit CLI naming bypasses `RepoScope.PYTHON_ONLY`.

    When the caller explicitly asks for `publish-pypi-action`, the component
    is materialized regardless of repo type, matching the existing scope-bypass
    semantics for awesome-only components.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.repomatic]\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    result = run_init(
        components=["publish-pypi-action"],
        output_dir=tmp_path,
        repo_slug="user/dotfiles",
    )

    created_set = set(result.created)
    assert ".github/actions/publish-pypi/action.yaml" in created_set


def test_include_config_bypasses_python_only_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`[tool.repomatic] include` bypasses `RepoScope.PYTHON_ONLY`."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.repomatic]\ninclude = ["publish-pypi-action"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/dotfiles")

    created_set = set(result.created)
    assert ".github/actions/publish-pypi/action.yaml" in created_set


def test_init_respects_exclude_label_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify exclude config with label file entries skips those label files."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["labels"]\n'
        'exclude = ["labels/labeller-content-based.yaml"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Excluded label file should not be created.
    assert ".github/labeller-content-based.yaml" not in created_set

    # Other label files should still be created.
    assert "labels.toml" in created_set
    assert ".github/labeller-file-based.yaml" in created_set


def test_init_mixed_exclude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify exclude with both component and file entries."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'exclude = ["workflows/debug.yaml", "skills/repomatic-audit"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    assert "labels.toml" not in created_set
    assert ".github/workflows/debug.yaml" not in created_set
    assert ".claude/skills/repomatic-audit/SKILL.md" not in created_set

    # Non-excluded items should be created.
    assert ".github/workflows/lint.yaml" in created_set
    assert ".claude/skills/repomatic-init/SKILL.md" in created_set


def test_init_detects_excluded_component_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify init reports excluded component files that still exist on disk."""
    # First init with labels included to create all files.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["labels"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path)

    labels_toml = tmp_path / "labels.toml"
    assert labels_toml.exists()

    # Now re-run without include — labels falls back to default exclusion.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )

    result = run_init(output_dir=tmp_path)

    # File is detected but not deleted (deletion requires --delete-excluded).
    assert labels_toml.exists()
    assert "labels.toml" in result.excluded_existing


def test_init_detects_excluded_skill_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify init reports a file-level excluded skill that still exists on disk."""
    # First create all skills (include overrides default exclusion).
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["skills"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path, repo_slug="user/awesome-list")

    skill_file = tmp_path / ".claude" / "skills" / "awesome-triage" / "SKILL.md"
    assert skill_file.exists()

    # Now exclude just that skill and re-run.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'exclude = ["skills/awesome-triage"]\n',
        encoding="UTF-8",
    )

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-list")

    # File is detected but not deleted.
    assert skill_file.exists()
    assert ".claude/skills/awesome-triage/SKILL.md" in result.excluded_existing
    # Other skills should still exist.
    assert (tmp_path / ".claude" / "skills" / "repomatic-init" / "SKILL.md").exists()


def test_init_detects_auto_excluded_awesome_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify awesome-triage is detected as excluded for non-awesome repos.

    When ``include`` does not cover skills, scope exclusions still apply
    and stale awesome-only files are flagged in ``excluded_existing``.
    """
    # Create skills including awesome-triage (as an awesome repo).
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["skills"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path, repo_slug="user/awesome-list")

    skill_file = tmp_path / ".claude" / "skills" / "awesome-triage" / "SKILL.md"
    assert skill_file.exists()

    # Re-run as a non-awesome repo without include covering skills.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\n',
        encoding="UTF-8",
    )
    result = run_init(output_dir=tmp_path, repo_slug="user/regular-project")

    # File is detected but not deleted.
    assert skill_file.exists()
    assert ".claude/skills/awesome-triage/SKILL.md" in result.excluded_existing


@pytest.mark.parametrize(
    ("location", "target", "expected"),
    [
        # Default location, no change.
        (
            "./.claude/skills/",
            ".claude/skills/foo/SKILL.md",
            ".claude/skills/foo/SKILL.md",
        ),
        # Equivalent default without leading "./".
        (
            ".claude/skills/",
            ".claude/skills/foo/SKILL.md",
            ".claude/skills/foo/SKILL.md",
        ),
        # Custom location replaces prefix.
        ("./custom/", ".claude/skills/foo/SKILL.md", "custom/foo/SKILL.md"),
        # Custom location without leading "./".
        ("custom/dir/", ".claude/skills/foo/SKILL.md", "custom/dir/foo/SKILL.md"),
        # Non-matching target is returned unchanged.
        ("./custom/", "other/path.md", "other/path.md"),
        # Hidden directory preserved with removeprefix (not strip).
        (
            "./.hidden/skills/",
            ".claude/skills/foo/SKILL.md",
            ".hidden/skills/foo/SKILL.md",
        ),
    ],
)
def test_resolve_skills_target(location: str, target: str, expected: str):
    """Verify skill target path resolution with various config values."""
    config = Config(skills_location=location)
    assert _resolve_skills_target(target, config) == expected


def test_resolve_skills_target_no_config():
    """Verify no-op when config is None."""
    assert (
        _resolve_skills_target(".claude/skills/x/SKILL.md", None)
        == ".claude/skills/x/SKILL.md"
    )


def test_init_skills_custom_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify skills are written to a custom location when configured."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'skills.location = "./custom/skills/"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    # Skills should be in the custom location.
    custom_skill = tmp_path / "custom" / "skills" / "repomatic-init" / "SKILL.md"
    assert custom_skill.exists()
    assert "custom/skills/repomatic-init/SKILL.md" in result.created

    # Default location should not exist.
    assert not (tmp_path / ".claude" / "skills" / "repomatic-init").exists()


@pytest.mark.parametrize(
    ("location", "target", "expected"),
    [
        # Default location, no change.
        (
            "./.claude/agents/",
            ".claude/agents/foo.md",
            ".claude/agents/foo.md",
        ),
        # Equivalent default without leading "./".
        (
            ".claude/agents/",
            ".claude/agents/foo.md",
            ".claude/agents/foo.md",
        ),
        # Custom location replaces prefix.
        ("./custom/", ".claude/agents/foo.md", "custom/foo.md"),
        # Custom location without leading "./".
        ("custom/dir/", ".claude/agents/foo.md", "custom/dir/foo.md"),
        # Non-matching target is returned unchanged.
        ("./custom/", "other/path.md", "other/path.md"),
        # Hidden directory preserved with removeprefix (not strip).
        (
            "./.hidden/agents/",
            ".claude/agents/foo.md",
            ".hidden/agents/foo.md",
        ),
    ],
)
def test_resolve_agents_target(location: str, target: str, expected: str):
    """Verify agent target path resolution with various config values."""
    config = Config(agents_location=location)
    assert _resolve_agents_target(target, config) == expected


def test_resolve_agents_target_no_config():
    """Verify no-op when config is None."""
    assert _resolve_agents_target(".claude/agents/x.md", None) == ".claude/agents/x.md"


def test_init_agents_custom_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify agents are written to a custom location when configured."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["agents"]\n'
        'agents.location = "./custom/agents/"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    # Agents should be in the custom location.
    custom_agent = tmp_path / "custom" / "agents" / "grunt-qa.md"
    assert custom_agent.exists()
    assert "custom/agents/grunt-qa.md" in result.created

    # Default location should not exist.
    assert not (tmp_path / ".claude" / "agents" / "grunt-qa.md").exists()


def test_init_detects_excluded_agent_custom_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify excluded agent detection works at custom locations."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["agents"]\n'
        'agents.location = "./custom/agents/"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path)

    agent_file = tmp_path / "custom" / "agents" / "grunt-qa.md"
    assert agent_file.exists()

    # Exclude that agent and re-run.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["agents"]\n'
        'exclude = ["agents/grunt-qa"]\n'
        'agents.location = "./custom/agents/"\n',
        encoding="UTF-8",
    )

    result = run_init(output_dir=tmp_path)

    assert agent_file.exists()
    assert "custom/agents/grunt-qa.md" in result.excluded_existing


def test_init_detects_excluded_skill_custom_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify excluded skill detection works at custom locations."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'skills.location = "./custom/skills/"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path, repo_slug="user/awesome-list")

    skill_file = tmp_path / "custom" / "skills" / "awesome-triage" / "SKILL.md"
    assert skill_file.exists()

    # Exclude that skill and re-run.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'exclude = ["skills/awesome-triage"]\n'
        'skills.location = "./custom/skills/"\n',
        encoding="UTF-8",
    )

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-list")

    assert skill_file.exists()
    assert "custom/skills/awesome-triage/SKILL.md" in result.excluded_existing


def test_init_detects_disabled_opt_in_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify disabled opt-in workflows on disk are detected as excluded."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        "notification.unsubscribe = true\n",
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    # Create the opt-in workflow while it's enabled.
    result = run_init(output_dir=tmp_path)
    wf = tmp_path / ".github" / "workflows" / "unsubscribe.yaml"
    assert wf.exists()
    assert ".github/workflows/unsubscribe.yaml" not in result.excluded_existing

    # Disable the opt-in workflow and re-run.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        "notification.unsubscribe = false\n",
        encoding="UTF-8",
    )

    result = run_init(output_dir=tmp_path)

    # File is detected as stale but not deleted.
    assert wf.exists()
    assert ".github/workflows/unsubscribe.yaml" in result.excluded_existing
    # Disabled opt-in workflows should not appear in "Excluded by config".
    assert "workflows/unsubscribe.yaml" not in result.excluded


def test_init_cli_delete_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify --delete-excluded deletes excluded files and cleans empty dirs."""
    from click.testing import CliRunner

    from repomatic.cli import repomatic

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["skills"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path, repo_slug="user/awesome-list")

    skill_file = tmp_path / ".claude" / "skills" / "awesome-triage" / "SKILL.md"
    assert skill_file.exists()

    # Exclude awesome-triage and re-run with --delete-excluded.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'exclude = ["skills/awesome-triage"]\n',
        encoding="UTF-8",
    )

    runner = CliRunner()
    cli_result = runner.invoke(
        repomatic,
        ["init", "--output-dir", str(tmp_path), "--delete-excluded"],
    )

    assert cli_result.exit_code == 0
    assert not skill_file.exists()
    # Empty parent directory should also be removed.
    assert not skill_file.parent.exists()
    assert "Deleted" in cli_result.output
    assert "excluded" in cli_result.output


def test_init_cli_no_delete_excluded_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify excluded files are reported but not deleted without --delete-excluded."""
    from click.testing import CliRunner

    from repomatic.cli import repomatic

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["skills"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path, repo_slug="user/awesome-list")

    skill_file = tmp_path / ".claude" / "skills" / "awesome-triage" / "SKILL.md"
    assert skill_file.exists()

    # Exclude awesome-triage and re-run without --delete-excluded.
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["skills"]\n'
        'exclude = ["skills/awesome-triage"]\n',
        encoding="UTF-8",
    )

    runner = CliRunner()
    cli_result = runner.invoke(
        repomatic,
        ["init", "--output-dir", str(tmp_path)],
    )

    assert cli_result.exit_code == 0
    # File is still on disk.
    assert skill_file.exists()
    assert "--delete-excluded" in cli_result.output


def test_init_explicit_components_bypass_exclude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify explicit CLI components override exclusion."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'exclude = ["labels", "skills", "changelog"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    # Explicitly request excluded components.
    result = run_init(output_dir=tmp_path, components=("labels", "changelog"))

    created_set = set(result.created)
    # Explicitly requested components should be created despite exclusion.
    assert "labels.toml" in created_set
    assert "changelog.md" in created_set

    # Exclusion list should be empty when explicit components given.
    assert result.excluded == []


def test_init_exclude_unknown_component_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify unknown component name in exclude raises ValueError."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'exclude = ["nonexistent-component"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="Unknown exclude"):
        run_init(output_dir=tmp_path)


def test_init_exclude_unknown_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify unknown file identifier in exclude raises ValueError."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'exclude = ["workflows/nonexistent.yaml"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="Unknown file"):
        run_init(output_dir=tmp_path)


def test_init_include_unknown_component_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify unknown component name in include raises ValueError."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["nonexistent-component"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="Unknown include"):
        run_init(output_dir=tmp_path)


def test_init_include_overrides_default_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify include overrides default exclusions additively."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["labels"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Labels included via include; agents, renovate, skills still excluded by default.
    assert "labels.toml" in created_set
    assert "renovate.json5" not in created_set
    for _, rel_path in ((e.source, e.target) for e in _BY_NAME["agents"].files):
        assert rel_path not in created_set
    for _, rel_path in ((e.source, e.target) for e in _BY_NAME["skills"].files):
        assert rel_path not in created_set
    assert result.excluded == ["agents", "renovate", "skills"]


def test_init_exclude_additive_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify user exclude is additive to default exclusions."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'exclude = ["workflows/debug.yaml"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Default exclusions (labels, skills) still apply.
    assert "labels.toml" not in created_set
    for _, rel_path in ((e.source, e.target) for e in _BY_NAME["skills"].files):
        assert rel_path not in created_set
    # User exclude is additive.
    assert ".github/workflows/debug.yaml" not in created_set
    assert "labels" in result.excluded
    assert "skills" in result.excluded
    assert "workflows/debug.yaml" in result.excluded


# --- Data file registry and exclude validation tests ---


def test_all_data_files_registered_in_exportable_files() -> None:
    """Every non-infrastructure file in data/ must appear in EXPORTABLE_FILES."""
    from importlib.resources import as_file, files

    data_dir = files("repomatic.data")
    with as_file(data_dir) as data_path:
        on_disk = {
            p.name
            for p in Path(data_path).iterdir()
            if p.name != "__init__.py"
            and not p.name.startswith(".")
            and not p.name.startswith("__")
            and not p.is_dir()
        }

    registered = set(EXPORTABLE_FILES.keys())
    unregistered = on_disk - registered
    assert not unregistered, (
        f"Data files not in EXPORTABLE_FILES: {sorted(unregistered)}"
    )


def test_every_data_file_maps_to_a_component() -> None:
    """Every file in EXPORTABLE_FILES belongs to a registry component, tool
    runner default, or registered runtime fragment.
    """
    # Collect all source filenames from the registry.
    registry_filenames: set[str] = set()
    for comp in COMPONENTS:
        for entry in comp.files:
            registry_filenames.add(entry.source)
        if isinstance(comp, ToolConfigComponent):
            registry_filenames.add(comp.source_file)

    # Collect bundled default configs used by the tool runner at runtime.
    bundled_defaults = {
        spec.default_config for spec in TOOL_REGISTRY.values() if spec.default_config
    }

    covered = registry_filenames | bundled_defaults | set(RUNTIME_FRAGMENTS)
    uncovered = set(EXPORTABLE_FILES.keys()) - covered
    assert not uncovered, (
        f"EXPORTABLE_FILES entries not mapped to any component: {sorted(uncovered)}"
    )


def test_no_data_file_claimed_by_multiple_components() -> None:
    """Each data filename must belong to at most one component."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []

    for comp in COMPONENTS:
        sources: list[str] = [e.source for e in comp.files]
        if isinstance(comp, ToolConfigComponent):
            sources.append(comp.source_file)
        for source_filename in sources:
            if source_filename in seen:
                duplicates.append(
                    f"{source_filename!r} claimed by both"
                    f" {seen[source_filename]!r} and {comp.name!r}"
                )
            seen[source_filename] = comp.name

    assert not duplicates, f"Duplicate file mappings: {duplicates}"


def testvalid_file_ids_cover_all_multi_file_components() -> None:
    """Components with multiple files must report valid file identifiers."""
    for component in ("workflows", "labels", "skills"):
        ids = valid_file_ids(component)
        assert ids, f"valid_file_ids({component!r}) returned empty set"


def test_workflow_file_ids_match_all_workflow_files() -> None:
    """valid_file_ids('workflows') must match ALL_WORKFLOW_FILES."""
    assert valid_file_ids("workflows") == frozenset(ALL_WORKFLOW_FILES)


def test_component_file_ids_match_registry() -> None:
    """valid_file_ids must return identifiers matching registry entries."""
    for comp in COMPONENTS:
        if not comp.files:
            continue
        expected = frozenset(entry.file_id for entry in comp.files)
        assert valid_file_ids(comp.name) == expected


def test_tool_components_have_no_file_ids() -> None:
    """Tool components (ruff, pytest, etc.) do not support file-level exclusion."""
    for c in COMPONENTS:
        if not isinstance(c, ToolConfigComponent):
            continue
        name = c.name
        assert valid_file_ids(name) == frozenset()


def test_workflow_files_target_workflow_dir() -> None:
    """All workflow file entries must target .github/workflows/."""
    for entry in _BY_NAME["workflows"].files:
        assert entry.target.startswith(".github/workflows/"), (
            f"Workflow entry {entry.file_id!r} targets {entry.target!r},"
            " expected .github/workflows/ prefix"
        )


def test_workflow_sources_are_yaml() -> None:
    """All workflow source files must be .yaml."""
    for entry in _BY_NAME["workflows"].files:
        assert entry.source.endswith(".yaml"), (
            f"Workflow entry {entry.file_id!r} source {entry.source!r}"
            " is not a .yaml file"
        )


def test_skill_files_target_skill_dir() -> None:
    """All skill file entries must target .claude/skills/{id}/SKILL.md."""
    for entry in _BY_NAME["skills"].files:
        assert entry.target.startswith(".claude/skills/"), (
            f"Skill entry {entry.file_id!r} targets {entry.target!r},"
            " expected .claude/skills/ prefix"
        )
        assert entry.target.endswith("/SKILL.md"), (
            f"Skill entry {entry.file_id!r} targets {entry.target!r},"
            " expected /SKILL.md suffix"
        )


def test_skill_sources_follow_naming_convention() -> None:
    """Skill source files must be named skill-{id}.md."""
    for entry in _BY_NAME["skills"].files:
        expected_source = f"skill-{entry.file_id}.md"
        assert entry.source == expected_source, (
            f"Skill {entry.file_id!r}: source is {entry.source!r},"
            f" expected {expected_source!r}"
        )


def test_skill_file_id_matches_target_dir() -> None:
    """Skill file_id must match the directory name in the target path."""
    for entry in _BY_NAME["skills"].files:
        parts = entry.target.split("/")
        # .claude/skills/{id}/SKILL.md → parts[2] is the id.
        assert parts[2] == entry.file_id, (
            f"Skill {entry.file_id!r}: target dir is {parts[2]!r}"
        )


def test_no_target_prefix_mixing_within_component() -> None:
    """Files within a component must not mix unrelated target directories."""
    for comp in COMPONENTS:
        if not comp.files:
            continue
        prefixes = {entry.target.split("/")[0] for entry in comp.files}
        # Allow mixing root files (no slash) with dotdir files within
        # a component (e.g., labels has both .github/ and root files).
        root_file = next(
            (entry.target for entry in comp.files if "/" not in entry.target),
            None,
        )
        if root_file is not None:
            prefixes.discard(root_file)
        assert len(prefixes) <= 1, (
            f"Component {comp.name!r} mixes target prefixes: {sorted(prefixes)}"
        )


def test_tool_config_rejects_missing_source_file() -> None:
    """ToolConfigComponent raises ValueError without source_file."""
    with pytest.raises(ValueError, match="requires source_file"):
        ToolConfigComponent(
            name="bad",
            description="test",
            tool_section="tool.bad",
        )


def test_tool_config_rejects_missing_tool_section() -> None:
    """ToolConfigComponent raises ValueError without tool_section."""
    with pytest.raises(ValueError, match="requires tool_section"):
        ToolConfigComponent(
            name="bad",
            description="test",
            source_file="bad.toml",
        )


def test_tool_config_rejects_files() -> None:
    """ToolConfigComponent raises ValueError with file entries."""
    from repomatic.registry import FileEntry

    with pytest.raises(ValueError, match="must not have files"):
        ToolConfigComponent(
            name="bad",
            description="test",
            source_file="bad.toml",
            tool_section="tool.bad",
            files=(FileEntry("bad.toml"),),
        )


def test_file_ids_unique_within_component() -> None:
    """File IDs must be unique within each component."""
    for comp in COMPONENTS:
        ids = [entry.file_id for entry in comp.files]
        assert len(ids) == len(set(ids)), (
            f"Component {comp.name!r} has duplicate file_ids:"
            f" {sorted(fid for fid in ids if ids.count(fid) > 1)}"
        )


def test_component_names_unique() -> None:
    """Component names must be unique across the registry."""
    names = [c.name for c in COMPONENTS]
    assert len(names) == len(set(names)), (
        f"Duplicate component names: {sorted(n for n in names if names.count(n) > 1)}"
    )


def test_config_key_has_config_default() -> None:
    """Entries with config_key must have an intentional config_default.

    File entries default to ``False`` (opt-in). Component entries default
    to ``True`` (opt-out). This test verifies the pairing exists — not the
    specific default value.
    """
    for comp in COMPONENTS:
        if comp.config_key:
            # Component-level config_key exists; config_default is declared.
            assert isinstance(comp.config_default, bool)
        for entry in comp.files:
            if entry.config_key:
                assert isinstance(entry.config_default, bool)


def test_tools_with_bundled_defaults_not_init_components() -> None:
    """Tools with a bundled default_config must not also be init components.

    The tool runner already falls back to the bundled config at runtime when no
    native config exists (Level 3 in resolve_config). Copying the same file
    into downstream repos via init would be redundant pollution.
    """
    tools_with_defaults = {
        name for name, spec in TOOL_REGISTRY.items() if spec.default_config
    }
    file_components = {
        c.name for c in COMPONENTS if not isinstance(c, ToolConfigComponent)
    }
    overlap = tools_with_defaults & file_components
    assert not overlap, (
        f"Tools with default_config should not be file components:"
        f" {sorted(overlap)}."
        " The tool runner already uses the bundled config as a fallback."
    )


def test_init_reports_unmodified_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Init reports native config files that match bundled defaults."""
    from repomatic.tool_runner import get_data_file_path

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)

    with get_data_file_path("yamllint.yaml") as bundled:
        content = bundled.read_text(encoding="UTF-8")
    (tmp_path / ".yamllint.yaml").write_text(content, encoding="UTF-8")

    result = run_init(output_dir=tmp_path)
    assert ".yamllint.yaml" in result.unmodified_configs


def test_init_no_unmodified_when_different(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Init does not flag modified config files as unmodified."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)

    (tmp_path / ".yamllint.yaml").write_text(
        "rules:\n  line-length:\n    max: 80\n", encoding="UTF-8"
    )

    result = run_init(output_dir=tmp_path)
    assert result.unmodified_configs == []


# ---------------------------------------------------------------------------
# find_unmodified_init_files
# ---------------------------------------------------------------------------


def test_find_unmodified_init_files_detects_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Init-managed file matching bundled default is flagged as unmodified."""
    monkeypatch.chdir(tmp_path)
    content = export_content("labels.toml")
    (tmp_path / "labels.toml").write_text(content.rstrip() + "\n", encoding="UTF-8")

    from repomatic.init_project import find_unmodified_init_files

    result = find_unmodified_init_files()
    paths = [p for _, p in result]
    assert "labels.toml" in paths


def test_find_unmodified_init_files_ignores_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Modified init-managed file is not flagged."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "labels.toml").write_text("# custom labels\n", encoding="UTF-8")

    from repomatic.init_project import find_unmodified_init_files

    result = find_unmodified_init_files()
    paths = [p for _, p in result]
    assert "labels.toml" not in paths


def test_find_unmodified_init_files_skips_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Skills are not checked for redundancy."""
    monkeypatch.chdir(tmp_path)
    content = export_content("skill-repomatic-audit.md")
    skill_dir = tmp_path / ".claude" / "skills" / "repomatic-audit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content.rstrip() + "\n", encoding="UTF-8")

    from repomatic.init_project import find_unmodified_init_files

    result = find_unmodified_init_files()
    paths = [p for _, p in result]
    assert not any("skills" in p for p in paths)


def test_find_unmodified_init_files_renovate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Renovate redundancy check uses export_content (with stripping)."""
    monkeypatch.chdir(tmp_path)
    content = export_content("renovate.json5")
    (tmp_path / "renovate.json5").write_text(content.rstrip() + "\n", encoding="UTF-8")

    from repomatic.init_project import find_unmodified_init_files

    result = find_unmodified_init_files()
    paths = [p for _, p in result]
    assert "renovate.json5" in paths


def test_find_unmodified_init_files_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Redundant files across multiple components are all detected."""
    monkeypatch.chdir(tmp_path)

    for source_name, rel_path in (
        ("labels.toml", "labels.toml"),
        ("labeller-file-based.yaml", ".github/labeller-file-based.yaml"),
        ("labeller-content-based.yaml", ".github/labeller-content-based.yaml"),
    ):
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = export_content(source_name)
        path.write_text(content.rstrip() + "\n", encoding="UTF-8")

    from repomatic.init_project import find_unmodified_init_files

    result = find_unmodified_init_files()
    paths = [p for _, p in result]
    assert "labels.toml" in paths
    assert ".github/labeller-file-based.yaml" in paths
    assert ".github/labeller-content-based.yaml" in paths


def test_init_skips_identical_config(tmp_path: Path):
    """Init skips writing when downstream file matches bundled content."""
    result1 = run_init(output_dir=tmp_path, components=("renovate",))
    assert "renovate.json5" in result1.created

    result2 = run_init(output_dir=tmp_path, components=("renovate",))
    assert "renovate.json5" not in result2.updated
    assert "renovate.json5" not in result2.created
    assert "renovate.json5" in result2.unmodified_configs


@pytest.mark.parametrize(
    "entry",
    [
        "workflows/debug.yaml",
        "workflows/tests.yaml",
        "workflows/autofix.yaml",
        "skills/repomatic-audit",
        "labels/labels.toml",
        "labels/labeller-content-based.yaml",
    ],
)
def test_parse_component_entries_accepts_valid_qualified_entries(entry: str) -> None:
    """Qualified component/file entries are accepted by parse_component_entries."""
    components, files = parse_component_entries([entry])
    assert not components
    component = entry.split("/")[0]
    file_id = entry.split("/")[1]
    assert file_id in files[component]


@pytest.mark.parametrize("component", sorted(ALL_COMPONENTS.keys()))
def test_parse_component_entries_accepts_all_bare_components(component: str) -> None:
    """Every component name is accepted as a bare exclude entry."""
    components, files = parse_component_entries([component])
    assert component in components
    assert not files


def test_parse_component_entries_bare_filename_without_component_fails() -> None:
    """Bare filenames like 'debug.yaml' must fail — qualified form required."""
    with pytest.raises(ValueError, match="Unknown entry"):
        parse_component_entries(["debug.yaml"])


def test_parse_component_entries_bare_skill_name_without_component_fails() -> None:
    """Bare skill identifiers like 'repomatic-audit' must fail."""
    with pytest.raises(ValueError, match="Unknown entry"):
        parse_component_entries(["repomatic-audit"])


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ("nonexistent", "Unknown entry"),
        ("workflows/nonexistent.yaml", "Unknown file"),
        ("labels/nonexistent.toml", "Unknown file"),
        ("skills/nonexistent-skill", "Unknown file"),
        ("ruff/something", "does not support file-level"),
        ("pytest/something", "does not support file-level"),
    ],
)
def test_parse_component_entries_rejects_invalid_entries(
    entry: str, match: str
) -> None:
    """Invalid entries produce hard ValueError failures."""
    with pytest.raises(ValueError, match=match):
        parse_component_entries([entry])


# --- Qualified CLI selection tests ---


def test_init_single_skill(tmp_path: Path):
    """Qualified entry creates only the specified skill."""
    result = run_init(output_dir=tmp_path, components=("skills/repomatic-topics",))
    assert result.created == [".claude/skills/repomatic-topics/SKILL.md"]


def test_init_multiple_qualified_same_component(tmp_path: Path):
    """Multiple qualified entries for same component creates all specified."""
    result = run_init(
        output_dir=tmp_path,
        components=("skills/repomatic-topics", "skills/repomatic-audit"),
    )
    created_set = set(result.created)
    assert created_set == {
        ".claude/skills/repomatic-topics/SKILL.md",
        ".claude/skills/repomatic-audit/SKILL.md",
    }


def test_init_bare_overrides_qualified(tmp_path: Path):
    """Bare component name includes all files, ignoring qualified filter."""
    result = run_init(
        output_dir=tmp_path,
        components=("skills", "skills/repomatic-topics"),
    )
    # All skills created: explicit component request bypasses scope.
    total_skills = len(_BY_NAME["skills"].files)
    assert len(result.created) == total_skills


def test_init_mixed_bare_and_qualified(tmp_path: Path):
    """Bare + qualified for different components work independently."""
    result = run_init(
        output_dir=tmp_path,
        components=("labels", "skills/repomatic-topics"),
    )
    created_set = set(result.created)
    assert "labels.toml" in created_set
    assert ".github/labeller-file-based.yaml" in created_set
    assert ".github/labeller-content-based.yaml" in created_set
    assert ".claude/skills/repomatic-topics/SKILL.md" in created_set
    assert len(created_set) == 4


def test_init_qualified_workflow(tmp_path: Path):
    """Single workflow file selection."""
    result = run_init(
        output_dir=tmp_path,
        components=("workflows/autofix.yaml",),
    )
    assert len(result.created) == 1
    assert result.created[0] == ".github/workflows/autofix.yaml"


def test_init_qualified_scope_bypassed_when_explicit(tmp_path: Path):
    """Explicit selection bypasses scope: awesome-triage created in non-awesome repo."""
    result = run_init(
        output_dir=tmp_path,
        components=("skills/awesome-triage",),
        repo_slug="user/some-project",
    )
    assert result.created == [".claude/skills/awesome-triage/SKILL.md"]


def test_init_qualified_selection_context_in_error():
    """Qualified selection uses 'selection' context in error messages."""
    with pytest.raises(ValueError, match="Unknown selection"):
        run_init(output_dir=Path("/tmp"), components=("nonexistent",))


def test_init_awesome_template_not_auto_included_with_explicit_components(
    tmp_path: Path,
):
    """awesome-template is not auto-included when explicit components are given."""
    result = run_init(
        output_dir=tmp_path,
        components=("skills/repomatic-topics",),
        repo_slug="user/awesome-list",
    )
    created_set = set(result.created)
    assert created_set == {".claude/skills/repomatic-topics/SKILL.md"}
