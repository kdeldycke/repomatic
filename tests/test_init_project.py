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

import hashlib
import re
import subprocess
from datetime import date, timedelta
from importlib.resources import as_file, files
from pathlib import Path

import pytest
import tomlrt
import yaml
from click.testing import CliRunner
from packaging.version import InvalidVersion, Version

from repomatic import __version__, init_project as ip
from repomatic.cli import repomatic
from repomatic.cli_setup import init_project
from repomatic.config import Config, WorkflowConfig, load_repomatic_config
from repomatic.init_project import (
    EXPORTABLE_FILES,
    RUNTIME_FRAGMENTS,
    _detect_removed_assets,
    _highest_upstream_pin,
    _select_cooldown_pin,
    _update_tool_config,
    adopted_ongoing_configs,
    default_version_pin,
    export_content,
    get_data_content,
    init_config,
    resolve_default_pin,
    run_init,
)
from repomatic.registry import (
    ALL_COMPONENTS,
    ALL_WORKFLOW_FILES,
    COMPONENTS,
    COMPONENTS_BY_NAME,
    RELEASE_ENGINE_WORKFLOWS,
    REMOVED_ASSETS,
    REUSABLE_WORKFLOWS,
    SKILL_FILENAME,
    SKILL_PHASE_ORDER,
    SKILL_PHASES,
    BundledComponent,
    FileEntry,
    GeneratedComponent,
    InitDefault,
    RemovedAsset,
    RepoScope,
    SyncMode,
    TemplateComponent,
    ToolConfigComponent,
    WorkflowComponent,
    _skill_dir,
    _skill_source,
    _skill_target,
    _subagent_target,
    parse_component_entries,
    valid_file_ids,
)
from repomatic.tool_registry import TOOL_REGISTRY
from repomatic.tool_runner import get_data_file_path, run_tool
from repomatic.version_sync import Candidate, UpstreamRefPin
from tests.conftest import skip_unless_tool_runs

# Convenience set for tests that check opt-in workflow membership.
_OPT_IN_IDS = frozenset(
    f.file_id for f in COMPONENTS_BY_NAME["workflows"].files if f.config_key
)


@pytest.fixture
def toml_file(tmp_path: Path):
    """Return a factory writing TOML content to a scratch `pyproject.toml`.

    Replaces the `NamedTemporaryFile(delete=False)` plus manual `unlink()`
    dance: pytest removes `tmp_path` whether or not the test passes, so a
    failing assertion no longer leaks the file. Each call gets its own
    directory, so a test may seed several projects.
    """
    made: list[Path] = []

    def write(content: str) -> Path:
        directory = tmp_path / f"project-{len(made)}"
        directory.mkdir()
        path = directory / "pyproject.toml"
        path.write_text(content, encoding="UTF-8")
        made.append(path)
        return path

    return write


@pytest.fixture
def pin_build(monkeypatch: pytest.MonkeyPatch):
    """Return a factory pinning the running build `init` reports itself as.

    `run_init` and `resolve_default_pin` read the package's `__version__` and
    `__git_tag_sha__` to decide which upstream ref to write downstream, so
    almost every pin test has to stage both.
    """

    def pin(
        version: str = "7.4.2",
        sha: str | None = "a" * 40,
        candidates: list[Candidate] | None = None,
        tag_sha: str | None = None,
    ) -> None:
        monkeypatch.setattr(ip, "__version__", version)
        monkeypatch.setattr(ip, "__git_tag_sha__", sha)
        if candidates is not None:
            monkeypatch.setattr(ip, "github_candidates", lambda _url: candidates)
        if tag_sha is not None:
            monkeypatch.setattr(
                ip,
                "resolve_tag_to_sha",
                lambda _url, tag: tag_sha if tag == "v7.4.1" else None,
            )

    return pin


# --- Bundled data and export tests ---


def _sync_tool_config(content: str, comp) -> str | None:
    """Drive `_update_tool_config` from raw content, as `init_config` does."""
    return _update_tool_config(tomlrt.loads(content), content, comp)


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
    ("scope", "is_awesome", "is_python", "is_package", "expected"),
    [
        # ALL matches every trait combination.
        ("ALL", False, False, False, True),
        ("ALL", False, True, False, True),
        ("ALL", False, True, True, True),
        ("ALL", True, False, False, True),
        # AWESOME_ONLY matches awesome repos regardless of Python status.
        ("AWESOME_ONLY", False, False, False, False),
        ("AWESOME_ONLY", False, True, False, False),
        ("AWESOME_ONLY", False, True, True, False),
        ("AWESOME_ONLY", True, False, False, True),
        # PYTHON_ONLY matches any Python repo, virtual project included.
        ("PYTHON_ONLY", False, False, False, False),
        ("PYTHON_ONLY", False, True, False, True),
        ("PYTHON_ONLY", False, True, True, True),
        ("PYTHON_ONLY", True, False, False, False),
        # PACKAGE_ONLY additionally rejects the virtual project.
        ("PACKAGE_ONLY", False, False, False, False),
        ("PACKAGE_ONLY", False, True, False, False),
        ("PACKAGE_ONLY", False, True, True, True),
        ("PACKAGE_ONLY", True, False, False, False),
    ],
)
def test_repo_scope_matches(
    scope: str, is_awesome: bool, is_python: bool, is_package: bool, expected: bool
) -> None:
    """Verify RepoScope.matches returns correct results for all combinations."""
    assert RepoScope[scope].matches(is_awesome, is_python, is_package) is expected


def test_init_help_lists_all_components() -> None:
    """Verify the init command help text lists every registered component."""
    help_text = init_project.help
    assert help_text is not None
    for name in ALL_COMPONENTS:
        assert name in help_text, f"Component {name!r} missing from init help text"


def test_supported_config_types() -> None:
    """Verify that expected config types are registered as ToolConfigComponent."""
    for name in ("mypy", "ruff", "pytest", "bumpversion", "typos"):
        assert isinstance(COMPONENTS_BY_NAME[name], ToolConfigComponent)


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
    comp = COMPONENTS_BY_NAME[config_type]
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
    comp = COMPONENTS_BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)
    content = export_content(comp.source_file)
    parsed = tomlrt.loads(content)
    assert isinstance(parsed, dict)


@pytest.mark.parametrize(
    "config_type",
    sorted(c.name for c in COMPONENTS if isinstance(c, ToolConfigComponent)),
)
def test_native_format_no_tool_prefix(config_type: str) -> None:
    """Verify that native format does not have [tool.X] prefix."""
    comp = COMPONENTS_BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)
    content = export_content(comp.source_file)
    parsed = tomlrt.loads(content)
    # Native format should NOT have a "tool" key at the root.
    assert "tool" not in parsed


def test_unknown_file_raises_error() -> None:
    """Verify that an unknown file raises ValueError."""
    with pytest.raises(ValueError, match="Unknown file"):
        export_content("nonexistent.toml")


@pytest.mark.parametrize("filename", sorted(EXPORTABLE_FILES))
def test_exportable_file_loadable(filename: str) -> None:
    """Verify every registered data file can be loaded (no dangling symlinks)."""
    content = export_content(filename)
    assert len(content) > 0


@pytest.mark.parametrize(
    "comp",
    [c for c in COMPONENTS if isinstance(c, ToolConfigComponent)],
    ids=lambda c: c.name,
)
def test_native_templates_separate_header(comp: ToolConfigComponent) -> None:
    """Verify a template's file-level header ends with a blank line.

    The separator is what {func}`~repomatic.init_project._strip_header_comments`
    keys on to tell a header apart from a comment documenting the first key,
    the two being otherwise identical: a comment run at the top of the file.
    Running the header straight into a key hides the key's own documentation
    behind the same rule that drops the header, so it never reaches a
    downstream `pyproject.toml`.
    """
    lines = export_content(comp.source_file).splitlines()
    header_end = 0
    while header_end < len(lines) and lines[header_end].lstrip().startswith("#"):
        header_end += 1
    if not header_end:
        return
    assert header_end < len(lines), f"{comp.source_file} is all header"
    assert not lines[header_end].strip(), (
        f"{comp.source_file}: header runs into {lines[header_end]!r}. "
        "Separate it with a blank line, or the first key's comment is dropped."
    )


@pytest.mark.parametrize(
    "comp",
    [c for c in COMPONENTS if isinstance(c, ToolConfigComponent)],
    ids=lambda c: c.name,
)
def test_strip_header_comments_keeps_key_comments(comp: ToolConfigComponent) -> None:
    """Verify stripping removes the header and every comment below it survives.

    The second half is the one that bites: skipping to the first non-comment
    line also passes the "header is gone" check, while silently taking the
    first key's documentation with it.
    """
    source = export_content(comp.source_file)
    stripped = ip._strip_header_comments(source)
    # Stripping only ever removes a prefix.
    assert source.endswith(stripped)
    assert "Native format" not in stripped

    lines = source.splitlines()
    header_end = 0
    while header_end < len(lines) and lines[header_end].lstrip().startswith("#"):
        header_end += 1
    # Every comment written below the header documents a key, so it belongs in
    # the `[tool.X]` section: a downstream pyproject.toml has no other source
    # of documentation for a key it did not write itself.
    for line in lines[header_end:]:
        if line.lstrip().startswith("#"):
            assert line in stripped, f"{comp.source_file}: dropped key comment {line!r}"


# Keys intentionally different between template and repomatic's own pyproject.toml.
# - ``exclude``: key exists only in the template (for downstream), not in own config.
# - ``superset``: every template list entry must appear in own config, but own may
#   have extras.
_TEMPLATE_EXCLUDE_KEYS: dict[str, frozenset[str]] = {
    "bumpversion": frozenset({"current_version"}),
    # The template ships the coverage ratchet disabled (`fail_under = 0`), so
    # adopting a repomatic release never fails a downstream build on a floor
    # its author never picked. This repository sets its own.
    "coverage": frozenset({"fail_under"}),
    "mypy": frozenset(),
    "pytest": frozenset({"addopts"}),
    "ruff": frozenset({"extend-include"}),
    "typos": frozenset(),
}
_TEMPLATE_SUPERSET_KEYS: dict[str, frozenset[str]] = {
    "bumpversion": frozenset({"files"}),
    "coverage": frozenset(),
    "mypy": frozenset(),
    "pytest": frozenset(),
    "ruff": frozenset(),
    "typos": frozenset({"extend-ignore-re"}),
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
    comp = COMPONENTS_BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)
    template = tomlrt.loads(export_content(comp.source_file))

    project_root = Path(__file__).resolve().parent.parent
    tool_sections = tomlrt.loads(
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


# --- Bundled tool-config template tests ---


def _template(filename: str) -> dict:
    """Parse a bundled tool-config template into a plain mapping."""
    return tomlrt.loads(export_content(filename))


def _lookup(parsed: dict, key: str):
    """Resolve a dotted key path through nested tables."""
    for part in key.split("."):
        parsed = parsed[part]
    return parsed


@pytest.mark.parametrize(
    ("template", "key", "expected"),
    (
        ("ruff.toml", "preview", True),
        ("ruff.toml", "fix", True),
        ("ruff.toml", "unsafe-fixes", False),
        ("ruff.toml", "show-fixes", True),
        ("ruff.toml", "lint.future-annotations", True),
        ("ruff.toml", "format.docstring-code-format", True),
        ("mypy.toml", "warn_unused_configs", True),
        ("mypy.toml", "warn_redundant_casts", True),
        ("mypy.toml", "warn_unused_ignores", True),
        ("mypy.toml", "warn_return_any", True),
        ("mypy.toml", "warn_unreachable", True),
        ("mypy.toml", "pretty", True),
        # Collection stays inside the test suite, so pytest never descends into
        # an installed copy of the package.
        ("pytest.toml", "testpaths", ["tests"]),
        ("pytest.toml", "xfail_strict", True),
        # Coverage measurement and reporting, moved out of the pytest addopts
        # so they sit where coverage.py itself reads them.
        ("coverage.toml", "run.branch", True),
        ("coverage.toml", "report.precision", 2),
        # The ratchet ships disabled: a downstream repo opts in by raising it.
        ("coverage.toml", "report.fail_under", 0),
    ),
)
def test_template_setting(template: str, key: str, expected) -> None:
    """A bundled template pins the setting every downstream repo inherits."""
    assert _lookup(_template(template), key) == expected


@pytest.mark.parametrize(
    ("template", "key", "member"),
    (
        ("ruff.toml", "lint.ignore", "D400"),
        ("ruff.toml", "lint.ignore", "ERA001"),
        ("pytest.toml", "addopts", "--durations=10"),
        ("pytest.toml", "addopts", "--cov-report=term"),
        ("pytest.toml", "addopts", "--numprocesses=auto"),
        ("pytest.toml", "addopts", "--import-mode=importlib"),
        ("bumpversion.toml", "current_version", "0"),
    ),
)
def test_template_list_contains(template: str, key: str, member: str) -> None:
    """A bundled template carries the entry downstream repos depend on."""
    assert member in _lookup(_template(template), key)


def test_pytest_template_registers_once_marker() -> None:
    """The `once` marker the test workflow filters on must be declared."""
    markers = _template("pytest.toml")["markers"]
    assert any(marker.startswith("once:") for marker in markers)


def test_bumpversion_template_has_required_settings() -> None:
    """Bumpversion needs these three keys to run unattended in CI."""
    parsed = _template("bumpversion.toml")
    assert {"current_version", "allow_dirty", "ignore_missing_files"} <= set(parsed)


def test_bumpversion_template_declares_files() -> None:
    """A bumpversion config with no file patterns would bump nothing."""
    files = _template("bumpversion.toml")["files"]
    assert isinstance(files, list)
    assert files


# --- pyproject.toml merging tests ---


BARE_PYPROJECT = '[project]\nname = "test"\nversion = "0.1.0"\n'
"""A minimal PEP 621 project, the starting point for every merge test."""


@pytest.mark.parametrize(
    ("config_type", "expected"),
    (
        pytest.param("mypy", "[tool.mypy]", id="root-section"),
        # tomlkit preserves the template's native dotted-key style.
        pytest.param("ruff", "lint.ignore", id="dotted-subsection"),
        pytest.param("bumpversion", "[[tool.bumpversion.files]]", id="array-section"),
        # The bumpversion template carries inline comments on its config values.
        pytest.param(
            "bumpversion",
            "# Update version in [project] section.",
            id="template-comment",
        ),
    ),
)
def test_init_config_merges_template(toml_file, config_type, expected) -> None:
    """Merging a template into a bare pyproject keeps its native TOML shape."""
    result = init_config(config_type, toml_file(BARE_PYPROJECT))
    assert result is not None
    assert expected in result


def test_init_config_lychee_preserves_other_sections(toml_file) -> None:
    """Lychee init merges [tool.lychee] without stripping unrelated sections."""
    result = init_config(
        "lychee",
        toml_file(
            "[tool.gitleaks]\n"
            "[tool.gitleaks.allowlist]\n"
            'description = "false positives"\n'
            'commits = ["abc123"]\n'
        ),
    )
    assert result is not None
    parsed = tomlrt.loads(result)
    # Lychee was added.
    assert "lychee" in parsed["tool"]


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        pytest.param("", set(), id="no-tool-table"),
        pytest.param('[project]\nname = "papaya"\n', set(), id="project-only"),
        pytest.param("[tool.typos]\n", {"typos"}, id="ongoing-adopted"),
        pytest.param("[tool.ruff]\n", set(), id="bootstrap-stays-out"),
        pytest.param("[tool.gitleaks]\n", set(), id="unmanaged-section"),
        pytest.param(
            "[tool.typos]\n[tool.uv]\n[tool.bumpversion]\n[tool.mypy]\n",
            {"typos", "uv", "bumpversion"},
            id="every-ongoing-config",
        ),
    ),
)
def test_adopted_ongoing_configs(
    tmp_path: Path, content: str, expected: set[str]
) -> None:
    """A section already on disk re-enters the bare-init set, if it is ONGOING.

    `EXPLICIT` keeps `init` from pushing a tool config onto a repository that
    never asked for one; it must not also stop the ongoing sync of a section the
    repository already carries. `BOOTSTRAP` templates stay out either way: the
    repository owns them outright after the first write.
    """
    (tmp_path / "pyproject.toml").write_text(content, encoding="UTF-8")
    assert adopted_ongoing_configs(tmp_path) == expected


def test_adopted_ongoing_configs_without_pyproject(tmp_path: Path) -> None:
    """A repository with no pyproject.toml adopts nothing."""
    assert adopted_ongoing_configs(tmp_path) == set()


def test_adopted_ongoing_configs_are_explicit_and_ongoing(tmp_path: Path) -> None:
    """Whatever the helper returns is an EXPLICIT, ONGOING tool config.

    Guards the invariant against a registry edit that flips a component's
    `init_default` or `sync_mode` without revisiting this selection path.
    """
    sections = "\n".join(
        f"[{comp.tool_section}]"
        for comp in COMPONENTS
        if isinstance(comp, ToolConfigComponent)
    )
    (tmp_path / "pyproject.toml").write_text(sections + "\n", encoding="UTF-8")

    adopted = adopted_ongoing_configs(tmp_path)
    assert adopted
    for name in adopted:
        comp = COMPONENTS_BY_NAME[name]
        assert isinstance(comp, ToolConfigComponent)
        assert comp.init_default is InitDefault.EXPLICIT
        assert comp.sync_mode is SyncMode.ONGOING


def test_uv_component_uses_overlay_ongoing() -> None:
    """The uv tool config is an ongoing overlay, not a full-section rebuild."""
    comp = COMPONENTS_BY_NAME["uv"]
    assert isinstance(comp, ToolConfigComponent)
    assert comp.tool_section == "tool.uv"
    assert comp.sync_mode == SyncMode.ONGOING
    assert comp.overlay is True


def test_init_config_uv_overlay_noop_when_pins_match(toml_file) -> None:
    """A [tool.uv] already carrying the canonical pins is a no-op.

    The overlay updates owned keys in place, so a section whose pins already
    match the template returns no change. A regression to the rebuild-and-graft
    path would reorder the owned keys ahead of the project's keys and return a
    diff, failing this fixpoint guard.
    """
    pins = tomlrt.loads(export_content("uv.toml"))
    content = (
        '[project]\nname = "papaya"\nversion = "1.0.0"\n\n'
        "[tool.uv]\n"
        f'required-version = "{pins["required-version"]}"\n'
        'dependency-groups.docs = { requires-python = ">= 3.14" }\n'
        'sources.mango = { git = "https://github.com/example/mango", branch = "main" }\n'
        f'exclude-newer = "{pins["exclude-newer"]}"\n'
        'exclude-newer-package = { mango = "0 day" }\n'
        'build-backend.module-root = ""\n'
    )
    path = toml_file(content)
    assert init_config("uv", path) is None


def test_init_config_uv_overlay_updates_stale_pins_in_place(toml_file) -> None:
    """Stale uv pins are updated in place, preserving order and local keys."""
    pins = tomlrt.loads(export_content("uv.toml"))
    content = (
        '[project]\nname = "papaya"\nversion = "1.0.0"\n\n'
        "[tool.uv]\n"
        'required-version = ">=0.10.0,<0.11"\n'
        'dependency-groups.docs = { requires-python = ">= 3.14" }\n'
        'sources.mango = { git = "https://github.com/example/mango", branch = "main" }\n'
        'exclude-newer = "3 days"\n'
        'exclude-newer-package = { mango = "0 day" }\n'
        'build-backend.module-root = ""\n'
    )
    path = toml_file(content)
    result = init_config("uv", path)
    assert result is not None
    uv = tomlrt.loads(result)["tool"]["uv"]
    # Owned pins moved to the canonical template values.
    assert uv["required-version"] == pins["required-version"]
    assert uv["exclude-newer"] == pins["exclude-newer"]
    # Key order is preserved, so the section stays a pyproject-fmt fixpoint.
    assert list(uv.keys()) == [
        "required-version",
        "dependency-groups",
        "sources",
        "exclude-newer",
        "exclude-newer-package",
        "build-backend",
    ]
    # Project-owned keys are untouched.
    assert uv["sources"]["mango"]["git"] == "https://github.com/example/mango"
    assert uv["exclude-newer-package"]["mango"] == "0 day"
    assert uv["build-backend"]["module-root"] == ""


def test_init_config_uv_overlay_appends_missing_pins(toml_file) -> None:
    """A [tool.uv] lacking the pins gets both appended, local keys intact."""
    pins = tomlrt.loads(export_content("uv.toml"))
    content = (
        '[project]\nname = "papaya"\nversion = "1.0.0"\n\n'
        "[tool.uv]\n"
        'sources.mango = { git = "https://github.com/example/mango", branch = "main" }\n'
        'exclude-newer-package = { mango = "0 day" }\n'
    )
    path = toml_file(content)
    result = init_config("uv", path)
    assert result is not None
    uv = tomlrt.loads(result)["tool"]["uv"]
    assert uv["required-version"] == pins["required-version"]
    assert uv["exclude-newer"] == pins["exclude-newer"]
    # Project-owned keys survive the overlay.
    assert uv["sources"]["mango"]["git"] == "https://github.com/example/mango"
    assert uv["exclude-newer-package"]["mango"] == "0 day"


def test_full_ruff_init(toml_file) -> None:
    """Verify full ruff config initialization."""
    result = init_config(
        "ruff",
        toml_file(BARE_PYPROJECT),
    )
    assert result is not None
    parsed = tomlrt.loads(result)

    assert "tool" in parsed
    assert "ruff" in parsed["tool"]
    assert parsed["tool"]["ruff"].get("preview") is True
    assert "lint" in parsed["tool"]["ruff"]
    assert "format" in parsed["tool"]["ruff"]


def test_adds_config_to_empty_pyproject(toml_file) -> None:
    """Verify that config is added to a pyproject.toml without the section."""
    path = toml_file(BARE_PYPROJECT)

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


def test_adds_bumpversion_with_array_sections(toml_file) -> None:
    """Verify that bumpversion [[files]] sections are transformed."""
    path = toml_file(BARE_PYPROJECT)

    try:
        result = init_config("bumpversion", path)
        assert result is not None
        assert "[tool.bumpversion]" in result
        assert "[[tool.bumpversion.files]]" in result
    finally:
        path.unlink()


def test_returns_none_if_section_exists(toml_file) -> None:
    """Verify that None is returned if the section already exists."""
    path = toml_file(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.ruff]\npreview = false\n'
    )

    try:
        result = init_config("ruff", path)
        assert result is None
    finally:
        path.unlink()


def test_preserves_existing_content(toml_file) -> None:
    """Verify that existing content is preserved when adding config."""
    original = '[project]\nname = "test"\nversion = "1.0.0"\n'
    path = toml_file(original)
    result = init_config("mypy", path)
    assert result is not None
    assert 'name = "test"' in result
    assert 'version = "1.0.0"' in result
    assert "[tool.mypy]" in result


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


# --- Cooldown-gated default pin ---

# Three releases: 7.4.0 and 7.4.1 sit outside an 8-day window on 2026-08-02
# (cutoff 2026-07-25), 7.4.2 is inside it.
_COOLDOWN_CANDIDATES = [
    Candidate("7.4.0", "2026-07-01", "v7.4.0"),
    Candidate("7.4.1", "2026-07-24", "v7.4.1"),
    Candidate("7.4.2", "2026-07-31", "v7.4.2"),
]

# The same three releases, dated so the verdict holds whatever the real clock
# says: 7.4.1 is always aged, 7.4.2 always fresh. Required by any test driving
# `run_init`, which owns the cooldown comparison internally and exposes no
# `today` hook to freeze, unlike `resolve_default_pin`.
_CLOCK_ROBUST_CANDIDATES = [
    Candidate("7.4.1", "2020-01-01", "v7.4.1"),
    Candidate("7.4.2", "2999-01-01", "v7.4.2"),
]


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        # The running version has cleared the window: pin it unchanged. This is
        # the CI sync-repomatic path (init runs at the already-pinned, aged
        # version), which must stay an idempotent no-op.
        pytest.param("7.4.1", None, id="aged-base-pins-itself"),
        # A --refresh landed on a version still inside the window: step back to
        # the newest release that has cleared it.
        pytest.param("7.4.2", "7.4.1", id="fresh-base-steps-back"),
        # The running version is not published (yet): fail open to it.
        pytest.param("9.9.9", None, id="absent-base-fails-open"),
    ],
)
def test_select_cooldown_pin(base, expected):
    result = _select_cooldown_pin(
        _COOLDOWN_CANDIDATES, base, timedelta(days=8), date(2026, 8, 2)
    )
    assert (result.version if result else None) == expected


def test_select_cooldown_pin_no_aged_predecessor():
    """A fresh base with no cleared predecessor has nothing to step back to."""
    candidates = [Candidate("7.4.2", "2026-07-31", "v7.4.2")]
    result = _select_cooldown_pin(
        candidates, "7.4.2", timedelta(days=8), date(2026, 8, 2)
    )
    assert result is None


def test_resolve_default_pin_aged_version_is_noop(monkeypatch, pin_build):
    """An aged running version pins itself with its build SHA, no step-back.

    Locks in the CI sync-repomatic invariant: init at the already-pinned version
    never moves the pin and never resolves a replacement SHA.
    """
    pin_build(version="7.4.1", candidates=_COOLDOWN_CANDIDATES)
    monkeypatch.setattr(
        ip,
        "resolve_tag_to_sha",
        lambda _url, _tag: pytest.fail("aged base must not resolve a new SHA"),
    )
    assert resolve_default_pin(Config(), today=date(2026, 8, 2)) == ("v7.4.1", "a" * 40)


def test_resolve_default_pin_steps_back_when_fresh(pin_build):
    """A fresh running version steps the pin back, re-resolves the SHA, and warns.

    The step-back note is appended to the caller-supplied warnings list so
    `run_init` can surface it in the init summary, not only in the log.
    """
    pin_build(candidates=_COOLDOWN_CANDIDATES, tag_sha="b" * 40)
    warnings: list[str] = []
    result = resolve_default_pin(Config(), today=date(2026, 8, 2), warnings=warnings)
    assert result == ("v7.4.1", "b" * 40)
    assert len(warnings) == 1
    assert "7.4.2" in warnings[0] and "v7.4.1" in warnings[0]


def test_resolve_default_pin_aged_version_leaves_warnings_empty(pin_build):
    """The no-op path appends no warning."""
    pin_build(version="7.4.1", candidates=_COOLDOWN_CANDIDATES)
    warnings: list[str] = []
    resolve_default_pin(Config(), today=date(2026, 8, 2), warnings=warnings)
    assert warnings == []


def test_resolve_default_pin_dev_version_skips_datasource(monkeypatch, pin_build):
    """An unreleased dev cut pins its base version without any network call."""
    pin_build(version="7.4.2.dev0", sha="")
    monkeypatch.setattr(
        ip,
        "github_candidates",
        lambda _url: pytest.fail("dev version must not hit the datasource"),
    )
    assert resolve_default_pin(Config(), today=date(2026, 8, 2)) == ("v7.4.2", None)


def test_resolve_default_pin_zero_window_skips_datasource(monkeypatch, pin_build):
    """A disabled cooldown (0 days) pins the running version without a lookup."""
    pin_build(sha="")
    monkeypatch.setattr(
        ip,
        "github_candidates",
        lambda _url: pytest.fail("disabled cooldown must not hit the datasource"),
    )
    config = Config(minimum_release_age="0 days")
    assert resolve_default_pin(config, today=date(2026, 8, 2)) == ("v7.4.2", None)


def test_resolve_default_pin_fails_open_when_datasource_empty(monkeypatch, pin_build):
    """An unreachable datasource falls back to the running version."""
    pin_build()
    monkeypatch.setattr(ip, "github_candidates", lambda _url: [])
    assert resolve_default_pin(Config(), today=date(2026, 8, 2)) == ("v7.4.2", "a" * 40)


def _write_caller(path: Path, version: str, sha: str | None = None) -> None:
    """Drop a minimal upstream thin-caller `uses:` ref at *path*."""
    ref = f"{sha} # v{version}" if sha else f"v{version}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"jobs:\n"
        f"  lint:\n"
        f"    uses: kdeldycke/repomatic/.github/workflows/lint.yaml@{ref}\n",
        encoding="UTF-8",
    )


def test_highest_upstream_pin_none_without_refs(tmp_path):
    """A repository carrying no upstream ref has no floor."""
    assert _highest_upstream_pin(tmp_path, "kdeldycke/repomatic") is None


def test_highest_upstream_pin_converges_stragglers(tmp_path):
    """The repository-wide maximum wins, so a lagging file cannot drag the pin down."""
    workflows = tmp_path / ".github" / "workflows"
    _write_caller(workflows / "lint.yaml", "7.4.2", "a" * 40)
    _write_caller(workflows / "docs.yaml", "7.0.0", "b" * 40)
    assert _highest_upstream_pin(tmp_path, "kdeldycke/repomatic") == UpstreamRefPin(
        "7.4.2", "a" * 40
    )


def test_highest_upstream_pin_prefers_a_hardened_ref(tmp_path):
    """At equal versions the SHA-pinned ref wins, so the floor never unpins one."""
    workflows = tmp_path / ".github" / "workflows"
    _write_caller(workflows / "autolock.yaml", "7.4.2")
    _write_caller(workflows / "lint.yaml", "7.4.2", "a" * 40)
    assert _highest_upstream_pin(tmp_path, "kdeldycke/repomatic") == UpstreamRefPin(
        "7.4.2", "a" * 40
    )


def test_highest_upstream_pin_reads_composite_actions(tmp_path):
    """Composite-action refs count too: `release.yaml` pins one alongside the lanes."""
    action = tmp_path / ".github" / "actions" / "publish-pypi" / "action.yaml"
    _write_caller(action, "7.4.2", "a" * 40)
    assert _highest_upstream_pin(tmp_path, "kdeldycke/repomatic") == UpstreamRefPin(
        "7.4.2", "a" * 40
    )


# The pin `init` resolves across the whole downstream lifecycle. `7.4.2` is
# inside the cooldown window on the reference date, `7.4.1` and `7.4.0` have
# cleared it, and the running build carries SHA `a…`.
@pytest.mark.parametrize(
    ("label", "base", "floor", "expected"),
    [
        (
            "bootstrap on an aged release pins it",
            "7.4.1",
            None,
            ("v7.4.1", "a" * 40),
        ),
        (
            "bootstrap on a fresh release steps back",
            "7.4.2",
            None,
            ("v7.4.1", "b" * 40),
        ),
        (
            "steady state on an aged pin is a no-op",
            "7.4.1",
            UpstreamRefPin("7.4.1", "c" * 40),
            ("v7.4.1", "a" * 40),
        ),
        (
            "steady state on a fresh pin is a no-op too",
            "7.4.2",
            UpstreamRefPin("7.4.2", "c" * 40),
            ("v7.4.2", "a" * 40),
        ),
        (
            "adopting an aged release moves the pin up",
            "7.4.1",
            UpstreamRefPin("7.4.0", "c" * 40),
            ("v7.4.1", "a" * 40),
        ),
        (
            "adopting a fresh release holds at the floor",
            "7.4.2",
            UpstreamRefPin("7.4.1", "c" * 40),
            ("v7.4.1", "c" * 40),
        ),
        (
            "adopting a fresh release still clears a lower floor",
            "7.4.2",
            UpstreamRefPin("7.4.0", "c" * 40),
            ("v7.4.1", "b" * 40),
        ),
        (
            "a deliberate rollback is honored",
            "7.4.0",
            UpstreamRefPin("7.4.2", "c" * 40),
            ("v7.4.0", "a" * 40),
        ),
    ],
)
def test_resolve_default_pin_lifecycle(label, base, floor, expected, monkeypatch):
    """The cooldown gates an adoption and never regresses a committed pin.

    The steady-state rows are the ones that matter in CI: `sync-repomatic` runs
    `init` at the pinned version on every push, so a cooldown verdict recomputed
    there would downgrade the repository for a full window after each bump.
    """
    monkeypatch.setattr(ip, "__version__", base)
    monkeypatch.setattr(ip, "__git_tag_sha__", "a" * 40)
    monkeypatch.setattr(ip, "github_candidates", lambda _url: _COOLDOWN_CANDIDATES)
    monkeypatch.setattr(
        ip,
        "resolve_tag_to_sha",
        lambda _url, tag: "b" * 40 if tag == "v7.4.1" else None,
    )
    result = resolve_default_pin(Config(), today=date(2026, 8, 2), floor=floor)
    assert result == expected, label


def test_resolve_default_pin_matching_floor_skips_datasource(monkeypatch, pin_build):
    """A pin equal to the running version is answered without a network call.

    Every CI `sync-repomatic` run takes this path, so it must not depend on the
    GitHub releases datasource being reachable.
    """
    pin_build()
    monkeypatch.setattr(
        ip,
        "github_candidates",
        lambda _url: pytest.fail("a pin equal to the running version needs no lookup"),
    )
    warnings: list[str] = []
    result = resolve_default_pin(
        Config(),
        today=date(2026, 8, 2),
        warnings=warnings,
        floor=UpstreamRefPin("7.4.2", "c" * 40),
    )
    assert result == ("v7.4.2", "a" * 40)
    assert warnings == []


def test_resolve_default_pin_matching_floor_keeps_on_disk_sha(pin_build):
    """A build with no tag SHA of its own falls back to the pin already on disk.

    Without the fallback, regenerating from a source checkout would rewrite a
    hardened `@<sha> # vX.Y.Z` ref into a bare tag pin, and nothing downstream
    re-hardens an upstream ref.
    """
    pin_build(sha="")
    assert resolve_default_pin(
        Config(),
        today=date(2026, 8, 2),
        floor=UpstreamRefPin("7.4.2", "c" * 40),
    ) == ("v7.4.2", "c" * 40)


def test_resolve_default_pin_held_at_floor_warns(pin_build):
    """Declining to adopt a fresh release is reported, naming the kept version."""
    pin_build(candidates=_COOLDOWN_CANDIDATES)
    warnings: list[str] = []
    resolve_default_pin(
        Config(),
        today=date(2026, 8, 2),
        warnings=warnings,
        floor=UpstreamRefPin("7.4.1", "c" * 40),
    )
    assert len(warnings) == 1
    assert "7.4.2" in warnings[0] and "v7.4.1" in warnings[0]


def test_run_init_held_at_floor_leaves_workflows_untouched(tmp_path, pin_build):
    """Declining the pin declines the caller content with it.

    `init` renders workflow bodies from the running wheel and substitutes only
    the `uses:` ref, so regenerating them under a held-back pin ships the new
    release's triggers, `concurrency` groups and `env:` against the old
    release's reusable-workflow surface. Half an adoption is worse than none:
    the caller half lands with nothing pinned to serve it.
    """
    pin_build(version="7.4.1", sha="c" * 40)
    run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)

    # Running version moves inside the cooldown window; the repo stays at 7.4.1.
    pin_build(version="7.4.2", candidates=_CLOCK_ROBUST_CANDIDATES)
    result = run_init(output_dir=tmp_path, components=("workflows",))

    touched = [path for path in result.created + result.updated if "workflows/" in path]
    assert not touched, f"held-back pin still rewrote {touched}"
    autofix = (tmp_path / ".github" / "workflows" / "autofix.yaml").read_text(
        encoding="UTF-8"
    )
    assert f"autofix.yaml@{'c' * 40} # v7.4.1" in autofix
    assert any("Leaving the workflows" in warning for warning in result.warnings)


def test_run_init_first_adoption_writes_workflows_despite_step_back(
    tmp_path, pin_build
):
    """A repository with no pin yet still gets workflows when the cooldown bites.

    The counterpart carve-out: holding the content back needs somewhere to
    stand, and a first-time adoption has no prior tree to keep. Skipping here
    would leave the repository with no workflows at all, so the pin/content
    skew stands as the lesser outcome.
    """
    pin_build(candidates=_CLOCK_ROBUST_CANDIDATES, tag_sha="b" * 40)
    result = run_init(output_dir=tmp_path, components=("workflows",))

    assert any("workflows/" in path for path in result.created)
    autofix = (tmp_path / ".github" / "workflows" / "autofix.yaml").read_text(
        encoding="UTF-8"
    )
    assert f"autofix.yaml@{'b' * 40} # v7.4.1" in autofix


def test_run_init_keeps_a_fresh_pin_already_on_disk(tmp_path, monkeypatch, pin_build):
    """Re-running `init` at the pinned version regenerates without downgrading.

    End-to-end shape of the CI lifecycle: a maintainer adopts a release on its
    publication day, then `sync-repomatic` re-runs `init` at that very version
    on every push. The pin must survive untouched.
    """
    pin_build()
    run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)
    monkeypatch.setattr(
        ip,
        "github_candidates",
        lambda _url: pytest.fail("a pin equal to the running version needs no lookup"),
    )
    run_init(output_dir=tmp_path, components=("workflows",))
    autofix = (tmp_path / ".github" / "workflows" / "autofix.yaml").read_text(
        encoding="UTF-8"
    )
    assert f"autofix.yaml@{'a' * 40} # v7.4.2" in autofix


def test_run_init_no_cooldown_skips_datasource(tmp_path, monkeypatch, pin_build):
    """--no-cooldown pins the running version and never consults the datasource."""
    pin_build(sha="")
    monkeypatch.setattr(
        ip,
        "github_candidates",
        lambda _url: pytest.fail("cooldown=False must not hit the datasource"),
    )
    run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)
    autofix = (tmp_path / ".github" / "workflows" / "autofix.yaml").read_text(
        encoding="UTF-8"
    )
    assert "kdeldycke/repomatic/.github/workflows/autofix.yaml@v7.4.2" in autofix


_PACKER_JOB = """
  # Pack the recipe archive and hand it to the release engine.
  build-packer:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v5
      - run: echo pack
"""
"""A downstream asset-building job, appended below the managed lanes.

Its `uses:` names no upstream workflow, so `extract_extra_jobs` reads it as a
consumer addition rather than a managed lane.
"""


def _workflow_tree(root: Path) -> dict[str, bytes]:
    """Every generated workflow file under *root*, keyed by relative path."""
    workflows = root / ".github" / "workflows"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(workflows.rglob("*"))
        if path.is_file()
    }


def _add_downstream_release_job(release: Path) -> None:
    """Append {data}`_PACKER_JOB` to *release* and gate the engine lane on it.

    Rewrites the `release` lane's `needs:` only: `publish-pypi` declares the
    same `needs: build` earlier in the file, so a blind replace would hit it.
    """
    head, marker, tail = release.read_text(encoding="UTF-8").partition("  release:\n")
    gated = tail.replace(
        "    needs: build\n",
        "    needs:\n      - build\n      - build-packer\n",
        1,
    )
    release.write_text(head + marker + gated + _PACKER_JOB, encoding="UTF-8")


def test_run_init_preserves_downstream_release_needs(tmp_path, pin_build):
    """`repomatic init` carries a consumer's own `needs:` edge across a sync.

    Asserted on the entry point `sync-repomatic` actually runs. The same
    invariant held for {meth}`WorkflowFormat.write_workflow` while this path
    silently dropped the edge, because the two rendered the caller through
    separate copies of the same logic and only one was ever wired up.
    """
    pin_build()
    run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)

    release = tmp_path / ".github" / "workflows" / "release.yaml"
    _add_downstream_release_job(release)
    run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)

    jobs = yaml.safe_load(release.read_text(encoding="UTF-8"))["jobs"]
    assert jobs["release"]["needs"] == ["build", "build-packer"]
    assert "build-packer" in jobs


def test_run_init_is_idempotent(tmp_path, pin_build):
    """A second `init` over its own output rewrites nothing.

    `sync-repomatic` re-runs `init` on every push to `main`, so any non-
    convergent render becomes an unbounded drift that opens a fresh PR forever.
    Checked with downstream extras in the tree, which is where the separator
    between the managed lanes and those extras is re-derived.
    """
    pin_build()
    run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)
    _add_downstream_release_job(tmp_path / ".github" / "workflows" / "release.yaml")

    # First re-run absorbs the hand edit; every run after it must be a no-op.
    run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)
    settled = _workflow_tree(tmp_path)

    result = run_init(output_dir=tmp_path, components=("workflows",), cooldown=False)
    drifted = sorted(
        name
        for name, content in _workflow_tree(tmp_path).items()
        if settled.get(name) != content
    )
    assert not drifted, f"non-convergent render: {drifted}"
    assert not result.updated


# ---------------------------------------------------------------------------
# Header-only sync of non-reusable workflows
# ---------------------------------------------------------------------------


_DOWNSTREAM_TESTS_YAML = (
    '---\nname: Old Name\n"on":\n  push:\n\njobs:\n\n'
    "  my-tests:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hello\n"
)
"""A downstream `tests.yaml`: canonical header gone stale, own jobs below it."""


def _init_workflow(tmp_path, monkeypatch, filename: str, config=None):
    """Run `init` for a single workflow file and return its content, if written."""
    monkeypatch.setattr(ip, "__version__", "7.4.2")
    monkeypatch.setattr(ip, "__git_tag_sha__", "a" * 40)
    run_init(
        output_dir=tmp_path,
        components=(f"workflows/{filename}",),
        cooldown=False,
        config=config,
    )
    target = tmp_path / ".github" / "workflows" / filename
    return target.read_text(encoding="UTF-8") if target.exists() else None


def test_init_syncs_header_and_keeps_downstream_jobs(tmp_path, monkeypatch):
    """A non-reusable workflow gets the canonical header, keeping its own jobs.

    `tests.yaml` has no `workflow_call` trigger, so it is never rendered as a
    caller: only everything above its `jobs:` line is replaced.
    """
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(_DOWNSTREAM_TESTS_YAML, encoding="UTF-8")

    content = _init_workflow(tmp_path, monkeypatch, "tests.yaml")

    assert content is not None
    assert "Old Name" not in content
    assert "concurrency:" in content
    assert "my-tests:" in content
    assert "echo hello" in content


def test_init_skips_a_non_reusable_workflow_absent_downstream(tmp_path, monkeypatch):
    """Header-only sync never creates a file, it only updates one already there.

    The downstream repo owns the jobs of a non-reusable workflow, so there is
    nothing to write when it has not adopted the workflow at all.
    """
    assert _init_workflow(tmp_path, monkeypatch, "tests.yaml") is None


@pytest.mark.parametrize(
    "pinned",
    (
        # Behind the refs, the lag `check_inline_pins_match_upstream` reports.
        "7.4.1",
        # Ahead of them, which is drift the same way.
        "7.5.0",
    ),
)
def test_init_realigns_an_inline_pin_in_downstream_jobs(tmp_path, monkeypatch, pinned):
    """The inline pin follows the `uses:` refs `init` writes, in either direction.

    The literal sits in a job body header-only sync never rewrites, so nothing
    else in this pass would touch it and the lint would go red until the next
    scheduled `sync-workflow-pins` run.
    """
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        _DOWNSTREAM_TESTS_YAML.replace(
            "echo hello",
            f"uvx --exclude-newer-package repomatic=P0D 'repomatic=={pinned}' metadata",
        ),
        encoding="UTF-8",
    )

    content = _init_workflow(tmp_path, monkeypatch, "tests.yaml")

    assert content is not None
    assert f"repomatic=={pinned}" not in content
    assert "'repomatic==7.4.2'" in content
    # The single-`=` cooldown escape hatch names a package, not a version.
    assert "--exclude-newer-package repomatic=P0D" in content


@pytest.mark.parametrize(
    "pinned",
    (
        # The pin moves, so the rewrite happens either way.
        "7.4.1",
        # Already at the target version: nothing to realign, and the exemption
        # is the only reason to write the file at all.
        "7.4.2",
    ),
)
def test_init_adds_a_missing_cooldown_exemption(tmp_path, monkeypatch, pinned):
    """A bare self-pin gets the exemption, or the command cannot resolve at all.

    Every workflow exports a `UV_EXCLUDE_NEWER`, and the pin `init` writes moves
    in lockstep with the `uses:` refs regardless of the cooldown, so it routinely
    names a release younger than that window. A bare `uvx 'repomatic==X.Y.Z'`
    left behind here fails to install the very version just written.
    """
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        _DOWNSTREAM_TESTS_YAML.replace(
            "echo hello", f"uvx --no-progress 'repomatic=={pinned}' metadata"
        ),
        encoding="UTF-8",
    )

    content = _init_workflow(tmp_path, monkeypatch, "tests.yaml")

    assert content is not None
    assert (
        "uvx --no-progress --exclude-newer-package repomatic=P0D "
        "'repomatic==7.4.2'" in content
    )


def test_init_warns_about_a_metadata_key_this_version_dropped(tmp_path, monkeypatch):
    """A workflow asking for a retired key is reported before the commit.

    An unknown key is a hard `UsageError`, so the mismatch takes down the
    metadata job and every job hanging off it on the first push.
    """
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        _DOWNSTREAM_TESTS_YAML.replace(
            "echo hello",
            "uvx 'repomatic==7.4.1' metadata harvest_yield test_matrix",
        ),
        encoding="UTF-8",
    )
    monkeypatch.setattr(ip, "__version__", "7.4.2")
    monkeypatch.setattr(ip, "__git_tag_sha__", "a" * 40)

    result = run_init(
        output_dir=tmp_path, components=("workflows/tests.yaml",), cooldown=False
    )

    assert len(result.warnings) == 1
    assert "'harvest_yield'" in result.warnings[0]
    # The keys that do exist are not named.
    assert "test_matrix" not in result.warnings[0]


def test_init_skips_the_metadata_key_check_behind_a_cooldown(tmp_path, monkeypatch):
    """A pin held back to an older release is judged by no key set at all.

    The older release's keys cannot be read from this process, and this
    version's would blame a workflow that works.
    """
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        _DOWNSTREAM_TESTS_YAML.replace(
            "echo hello", "uvx 'repomatic==7.4.1' metadata harvest_yield"
        ),
        encoding="UTF-8",
    )
    monkeypatch.setattr(ip, "__version__", "7.4.2")
    monkeypatch.setattr(ip, "__git_tag_sha__", "a" * 40)

    result = run_init(
        output_dir=tmp_path,
        components=("workflows/tests.yaml",),
        cooldown=False,
        version="v7.4.1",
    )

    assert result.warnings == []


def test_init_leaves_an_unrelated_pypi_pin_alone(tmp_path, monkeypatch):
    """Only the upstream toolkit's own literal is realigned, not every `==` pin."""
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        _DOWNSTREAM_TESTS_YAML.replace("echo hello", "uvx 'some-other-tool==1.2.3'"),
        encoding="UTF-8",
    )

    content = _init_workflow(tmp_path, monkeypatch, "tests.yaml")

    assert content is not None
    assert "some-other-tool==1.2.3" in content


def test_init_leaves_a_headerless_workflow_untouched(tmp_path, monkeypatch):
    """Without a `jobs:` line there is no boundary to splice the header onto."""
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: No Jobs\n", encoding="UTF-8")

    assert _init_workflow(tmp_path, monkeypatch, "tests.yaml") == "---\nname: No Jobs\n"


def test_init_header_sync_honors_paths_spec(tmp_path, monkeypatch):
    """The synced header carries the repository's own `paths:` filters."""
    target = tmp_path / ".github" / "workflows" / "tests.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        '---\nname: stub\n"on":\n  push:\n    paths:\n      - placeholder\njobs:\n'
        "  stub:\n    runs-on: ubuntu-latest\n",
        encoding="UTF-8",
    )
    config = Config(workflow=WorkflowConfig(extra_paths=["recipe-specific.sh"]))

    content = _init_workflow(tmp_path, monkeypatch, "tests.yaml", config=config)

    assert content is not None
    assert "recipe-specific.sh" in content
    assert "placeholder" not in content


def test_run_init_cooldown_steps_back_in_generated_pin(
    tmp_path, monkeypatch, pin_build
):
    """A fresh running version lands the stepped-back tag in the thin caller."""
    pin_build(sha="")
    # Dates chosen to be robust against the real clock resolve_default_pin
    # reads: 7.4.1 is always aged, 7.4.2 always fresh.
    monkeypatch.setattr(
        ip,
        "github_candidates",
        lambda _url: [
            Candidate("7.4.1", "2020-01-01", "v7.4.1"),
            Candidate("7.4.2", "2999-01-01", "v7.4.2"),
        ],
    )
    monkeypatch.setattr(ip, "resolve_tag_to_sha", lambda _url, _tag: "")
    result = run_init(output_dir=tmp_path, components=("workflows",))
    autofix = (tmp_path / ".github" / "workflows" / "autofix.yaml").read_text(
        encoding="UTF-8"
    )
    assert "kdeldycke/repomatic/.github/workflows/autofix.yaml@v7.4.1" in autofix
    # The step-back surfaces in the init summary, not only the log.
    assert any("v7.4.1" in w for w in result.warnings)


def test_init_default_components():
    """Verify default selection includes expected components."""
    defaults = {
        c.name
        for c in COMPONENTS
        if c.init_default in (InitDefault.INCLUDE, InitDefault.EXCLUDE)
    }
    assert "changelog" in defaults
    assert "labels" in defaults
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
        'include = ["skills", "subagents"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    # All components: agents, changelog, skills, workflows. Opt-in workflows
    # are excluded by default, and ephemeral components never materialize on a
    # bare init. Awesome-only skills are included because ``include =
    # ["skills"]`` bypasses scope filtering.
    config_file_count = sum(
        len(c.files)
        for c in COMPONENTS
        if isinstance(c, BundledComponent) and not c.ephemeral
    )
    opt_in_count = sum(1 for f in COMPONENTS_BY_NAME["workflows"].files if f.config_key)
    default_workflows = len(REUSABLE_WORKFLOWS) - opt_in_count
    expected_count = default_workflows + config_file_count + 1
    assert len(result.created) == expected_count
    assert len(result.skipped) == 0
    assert len(result.warnings) == 0

    # Verify workflow files exist (excluding opt-in workflows).
    for filename in REUSABLE_WORKFLOWS:
        if filename not in _OPT_IN_IDS:
            assert (tmp_path / ".github" / "workflows" / filename).exists()

    # The ephemeral label definitions stay out of the tree until named
    # explicitly. The two `labeller-*.yaml` rule files are never written at
    # all now: `apply-labels` reads them straight out of the package.
    assert not (tmp_path / "labels.toml").exists()
    assert not (tmp_path / ".github" / "labeller-file-based.yaml").exists()
    assert not (tmp_path / ".github" / "labeller-content-based.yaml").exists()

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
    config = Config(
        workflow=WorkflowConfig(
            extra_paths=["repo-specific.sh"],
            ignore_paths=["uv.lock"],
            paths={"docs.yaml": ["only-this.json5"]},
        ),
    )
    run_init(
        output_dir=tmp_path,
        components=("workflows",),
        config=config,
    )

    # Per-workflow override replaces the docs.yaml caller's paths.
    docs = (tmp_path / ".github" / "workflows" / "docs.yaml").read_text(
        encoding="UTF-8",
    )
    assert "only-this.json5" in docs
    # Override skips global knobs: extras don't leak to overridden workflows.
    assert "repo-specific.sh" not in docs

    # changelog.yaml has push.paths in canonical and is not overridden, so
    # global knobs apply.
    changelog = (tmp_path / ".github" / "workflows" / "changelog.yaml").read_text(
        encoding="UTF-8",
    )
    assert "repo-specific.sh" in changelog
    assert "uv.lock" not in changelog
    # Untouched canonical entries survive.
    assert "changelog.md" in changelog


def test_init_workflow_sync_disabled_leaves_workflows_untouched(tmp_path: Path):
    """`[tool.repomatic.workflow] sync = false` skips workflow generation.

    Like the other `*.sync` toggles, the opt-out writes nothing and leaves any
    existing workflow file untouched rather than overwriting or deleting it.
    Applies even when `workflows` is named explicitly.
    """
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    custom = workflows_dir / "lint.yaml"
    custom.write_text("# hand-written\n", encoding="UTF-8")

    config = Config(workflow=WorkflowConfig(sync=False))
    result = run_init(
        output_dir=tmp_path,
        components=("workflows",),
        config=config,
    )

    touched = [
        p
        for p in (*result.created, *result.updated)
        if p.startswith(".github/workflows/")
    ]
    assert touched == []
    assert custom.read_text(encoding="UTF-8") == "# hand-written\n"


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
    assert created_set == {"labels.toml"}

    # No workflows or changelog should be created.
    for filename in REUSABLE_WORKFLOWS:
        assert f".github/workflows/{filename}" not in created_set
    assert "changelog.md" not in created_set


def test_init_only_skills(tmp_path: Path):
    """Verify only skill files are created.

    Scope exclusions are bypassed when components are explicitly requested, so
    every skill is created, awesome-only ones included. The count is read off
    the registry rather than written here: a literal makes adding a skill fail
    a test that has nothing to say about it.
    """
    result = run_init(output_dir=tmp_path, components=("skills",))

    created_set = set(result.created)
    assert len(created_set) == len(COMPONENTS_BY_NAME["skills"].files)

    # Verify all skill files are created, including awesome-only ones.
    for name in (
        "av-false-positive",
        "awesome-triage",
        "babysit-ci",
        "benchmark-update",
        "brand-assets",
        "file-bug-report",
        "github-housekeeping",
        "repomatic-audit",
        "repomatic-changelog",
        "repomatic-deps",
        "repomatic-init",
        "repomatic-ship",
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
    component_skills = {entry.file_id for entry in COMPONENTS_BY_NAME["skills"].files}

    # Collect skills registered in SKILL_PHASES.
    phase_skills = set(SKILL_PHASES)

    # Collect data symlinks.
    data_dir = Path(__file__).resolve().parents[1] / "repomatic" / "data"
    data_skills = {p.parent.name for p in data_dir.glob("skills/*/SKILL.md")}

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


def test_every_skill_phase_is_orderable():
    """`list-skills` orders its rows by `SKILL_PHASE_ORDER.index()`.

    The two constants are declared apart: one names the phase of each registry
    entry, the other the lifecycle order they render in. A phase in the first
    and not the second used to drop its skill from the listing without a word,
    and now raises instead, so the roster can no longer go quietly short.
    """
    strays = set(SKILL_PHASES.values()) - set(SKILL_PHASE_ORDER)
    assert not strays, f"Phases with no rank in SKILL_PHASE_ORDER: {sorted(strays)}."


def test_init_only_workflows(tmp_path: Path):
    """Verify only workflow files are created.

    Pinned to a default `Config` rather than the ambient one: config discovery
    walks up from the working directory, so this repository's own
    `[tool.repomatic]` would otherwise decide which opt-in workflows the
    assertions below expect, and enabling one here would fail the test.
    """
    result = run_init(output_dir=tmp_path, components=("workflows",), config=Config())

    created_set = set(result.created)
    # Opt-in workflows are excluded by default.
    for filename in REUSABLE_WORKFLOWS:
        if filename in _OPT_IN_IDS:
            assert f".github/workflows/{filename}" not in created_set
        else:
            assert f".github/workflows/{filename}" in created_set

    # No config files or changelog.
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
    parsed = tomlrt.loads(get_data_content("typos.toml"))
    identifiers = parsed["default"]["extend-identifiers"]
    return {str(k): str(v) for k, v in identifiers.items()}


def _make_pyproject_with_template_bumpversion(
    toml_file, version: str = "7.5.3.dev0"
) -> str:
    """Generate a pyproject.toml with the bumpversion section from the template.

    Used as a fixture for tests that need an already-up-to-date config.
    """
    base = f'[project]\nname = "test-project"\nversion = "{version}"\n'
    result = init_config("bumpversion", toml_file(base))
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


def test_skips_already_up_to_date(tmp_path: Path, toml_file) -> None:
    """Verify config matching the template returns None (no changes needed)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        _make_pyproject_with_template_bumpversion(toml_file), encoding="UTF-8"
    )

    result = init_config("bumpversion", pyproject)

    assert result is None


def _tool_section(text: str, tool_section: str) -> str:
    """Extract the textual `[tool.X]` block, including any `[[...]]` sub-tables.

    `tool_section` is the dotted path without brackets (like ``tool.bumpversion``).
    Spans from the header to the next unrelated table, preserving key order,
    indentation, and comments (unlike an order-blind parsed-dict view).
    """
    header = f"[{tool_section}]"
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if not lines[i].startswith("["):
            continue
        body = lines[i].lstrip("[")
        if body == f"{tool_section}]" or body.startswith(f"{tool_section}."):
            continue
        end = i
        break
    return "\n".join(lines[start:end]).rstrip()


def test_bumpversion_template_is_pyproject_fmt_fixed_point(tmp_path: Path) -> None:
    """`sync-bumpversion` output must already be a fixed point of pyproject-fmt.

    `sync-bumpversion` rewrites `[tool.bumpversion]` from the bundled template,
    while `format-pyproject` reorders the same keys with pyproject-fmt. When the
    template's key order diverges from pyproject-fmt's, the two autofix jobs
    ping-pong: one PR imposes the template order, the next reverts it.

    The in-tree `pyproject.toml` has been processed by `format-pyproject`, so its
    `[tool.bumpversion]` block is the canonical pyproject-fmt output. Asserting the
    freshly merged template equals it textually (modulo `current_version`, the only
    preserved key) locks the order. Unlike `test_template_matches_own_pyproject`,
    which compares parsed dicts, this compares text, so it catches key-order drift.
    See `claude.md` § "Generator/formatter ping-pong is recurrent".
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test-project"\nversion = "1.0.0.dev0"\n', encoding="UTF-8"
    )
    merged = init_config("bumpversion", pyproject)
    assert merged is not None

    own = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="UTF-8"
    )

    def normalize(toml_text: str) -> str:
        section = _tool_section(toml_text, "tool.bumpversion")
        return re.sub(r'current_version = "[^"]*"', "current_version = ", section)

    assert normalize(merged) == normalize(own), (
        "Bundled bumpversion template diverges from the pyproject-fmt-formatted "
        "[tool.bumpversion] in pyproject.toml. sync-bumpversion and format-pyproject "
        "will ping-pong on every push. Reorder repomatic/data/bumpversion.toml to "
        "match pyproject-fmt's output."
    )


@pytest.mark.once
@pytest.mark.parametrize(
    "config_type",
    sorted(
        c.name
        for c in COMPONENTS
        if isinstance(c, ToolConfigComponent) and c.sync_mode is SyncMode.ONGOING
    ),
)
def test_ongoing_sync_template_survives_pyproject_fmt(
    config_type: str, tmp_path: Path
) -> None:
    """Templates re-synced into pyproject.toml must be pyproject-fmt fixed points.

    A config that `repomatic init` re-merges on every push (``sync_mode=ONGOING``,
    via the `sync-bumpversion` job or a bare `repomatic init` in `sync-repomatic`)
    shares `pyproject.toml` with `format-pyproject`. If its bundled template is not
    already in pyproject-fmt's canonical form, the two autofix jobs ping-pong: one
    rewrites the section, the next reformats it.

    This merges each template into a throwaway `pyproject.toml`, runs the pinned
    pyproject-fmt over it, and asserts the `[tool.X]` block is unchanged. It is the
    category guard for the bumpversion ↔ format-pyproject loop, and also covers
    scope-specific templates (lychee, opted into a Python project) that have no
    in-tree section to compare against. See `claude.md` § "Generator/formatter
    ping-pong is recurrent".

    Marked `once`: it shells out to pyproject-fmt via uvx, so a single runner
    suffices. Skipped when pyproject-fmt cannot be fetched (offline local dev);
    CI is the enforcement point.
    """
    comp = COMPONENTS_BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test-project"\nversion = "1.0.0"\n', encoding="UTF-8"
    )
    merged = init_config(config_type, pyproject)
    assert merged is not None
    pyproject.write_text(merged, encoding="UTF-8")

    before = _tool_section(merged, comp.tool_section)
    version = TOOL_REGISTRY["pyproject-fmt"].version
    try:
        result = subprocess.run(
            ["uvx", "--no-progress", f"pyproject-fmt=={version}", str(pyproject)],
            check=False,
            capture_output=True,
            encoding="UTF-8",
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"pyproject-fmt {version} unavailable: {exc}")
    # pyproject-fmt returns 0 (no change) or 1 (reformatted); anything else means
    # it could not run (e.g. uvx failed to fetch it), so skip rather than misread
    # an unmodified file as a passing fixed point.
    if result.returncode not in (0, 1):
        pytest.skip(f"pyproject-fmt {version} could not run: {result.stderr}")
    after = _tool_section(pyproject.read_text(encoding="UTF-8"), comp.tool_section)

    assert before == after, (
        f"pyproject-fmt {version} rewrites [{comp.tool_section}] after it is merged "
        f"from {comp.source_file}. Because this config is re-synced into "
        f"pyproject.toml on every push, it will ping-pong with format-pyproject. "
        f"Reformat repomatic/data/{comp.source_file} to match pyproject-fmt's output."
    )


@pytest.mark.parametrize(
    "config_type",
    sorted(
        c.name
        for c in COMPONENTS
        if isinstance(c, ToolConfigComponent) and c.sync_mode is SyncMode.ONGOING
    ),
)
def test_ongoing_resync_preserves_local_string_style(
    config_type: str, tmp_path: Path
) -> None:
    """Re-syncing an adopted config must not restyle what the project wrote.

    The sibling `test_ongoing_sync_template_survives_pyproject_fmt` merges each
    template into an *empty* file, so it only ever sees the template's own text.
    The ping-pong this guards needs a **local addition**: iterating a tomlrt
    array yields decoded values rather than the nodes carrying their source
    lexeme, so rebuilding the array re-emits every item in tomlrt's style. A
    local `'base32 "[0-9a-z]{52}"'`, which pyproject-fmt writes as a literal
    string precisely because it contains a quote, came back as an escaped
    `"base32 \\"[0-9a-z]{52}\\""`. Same content, so `format-pyproject` rewrote it
    straight back, and the two unattended jobs opened pull requests undoing each
    other indefinitely.

    Offline by construction: it asserts the invariant that makes the file a
    fixed point rather than shelling out to pyproject-fmt to observe one.
    """
    comp = COMPONENTS_BY_NAME[config_type]
    assert isinstance(comp, ToolConfigComponent)

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test-project"\nversion = "1.0.0"\n', encoding="UTF-8"
    )
    merged = init_config(config_type, pyproject)
    assert merged is not None

    # Splice a literal-quoted item into the first single-line array the template
    # defines. A template carrying none cannot regress this way.
    local = """'weather "[a-z]{3}"'"""
    spliced, count = re.subn(
        r"^(?P<key>[\w.-]+ = \[)(?P<body>[^\[\]\n]*)\]$",
        lambda m: f"{m['key']}{m['body'].rstrip()}, {local} ]",
        merged,
        count=1,
        flags=re.MULTILINE,
    )
    if not count:
        pytest.skip(f"{comp.source_file} defines no single-line array to graft into.")
    pyproject.write_text(spliced, encoding="UTF-8")
    before = _tool_section(spliced, comp.tool_section)

    resynced = init_config(config_type, pyproject)
    after = _tool_section(resynced or spliced, comp.tool_section)

    assert local in after, (
        f"Re-syncing [{comp.tool_section}] re-serialized a local array item, "
        f"turning {local} into something else. The project's own string style "
        f"must survive a re-sync, or format-pyproject and sync-repomatic will "
        f"each keep opening a pull request undoing the other's."
    )
    assert before == after


def _bumpversion_pyproject(*file_entries: str) -> str:
    """Render a pyproject whose `[tool.bumpversion]` carries *file_entries*.

    The table header is fixed across these tests; what each one is actually
    about is the `[[tool.bumpversion.files]]` entries it seeds, so only those
    appear at the call site.
    """
    header = (
        '[project]\nname = "test"\nversion = "1.0.0.dev0"\n\n'
        "[tool.bumpversion]\n"
        'current_version = "1.0.0.dev0"\n'
        "allow_dirty = true\n"
        'parse = "(?P<major>\\\\d+)"\n'
        'serialize = ["{major}"]\n\n'
    )
    return header + "".join(file_entries)


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
    parsed = tomlrt.loads(result)
    bv = parsed["tool"]["bumpversion"]
    assert "parse" in bv
    assert "serialize" in bv
    assert "parts" in bv
    assert "dev" in bv["parts"]


def test_preserves_local_array_entries(tmp_path: Path) -> None:
    """Verify local [[tool.bumpversion.files]] entries survive ongoing sync."""
    content = _bumpversion_pyproject(
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

    bv = COMPONENTS_BY_NAME["bumpversion"]
    assert isinstance(bv, ToolConfigComponent)
    result = _sync_tool_config(content, bv)

    assert result is not None
    parsed = tomlrt.loads(result)
    files_entries = parsed["tool"]["bumpversion"]["files"]
    # Local entry targeting readme.md must survive.
    readme_entries = [e for e in files_entries if e.get("filename") == "./readme.md"]
    assert len(readme_entries) == 1
    assert readme_entries[0]["search"] == "raw.githubusercontent.com/test/main/"
    assert readme_entries[0]["ignore_missing_version"] is True


def test_ongoing_sync_idempotent_with_local_entries(tmp_path: Path) -> None:
    """Verify ongoing sync with local entries is idempotent after first run."""
    content = _bumpversion_pyproject(
        "[[tool.bumpversion.files]]\n"
        'filename = "./readme.md"\n'
        "ignore_missing_version = true\n"
        'search = "example.com/main/"\n'
        'replace = "example.com/v{new_version}/"\n'
    )
    bv_comp = COMPONENTS_BY_NAME["bumpversion"]
    assert isinstance(bv_comp, ToolConfigComponent)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    # First run: updates template entries.
    result1 = _sync_tool_config(content, bv_comp)
    assert result1 is not None
    pyproject.write_text(result1, encoding="UTF-8")

    # Second run: should be a no-op.
    result2 = _sync_tool_config(result1, bv_comp)
    assert result2 is None


def test_ongoing_sync_no_duplicate_template_entries(tmp_path: Path) -> None:
    """Verify template entries are not duplicated when also present locally."""
    content = PYPROJECT_WITH_BUMPVERSION
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    parsed = tomlrt.loads(result)
    files_entries = parsed["tool"]["bumpversion"]["files"]
    # The template's three pyproject.toml entries ([project] version, download
    # URL, nuitka file-/product-version). The fixture's unanchored [project]
    # entry shares the version slot and is superseded by the template, not added
    # as a fourth entry.
    pyproject_entries = [
        e for e in files_entries if e.get("filename") == "./pyproject.toml"
    ]
    assert len(pyproject_entries) == 3


def test_evolved_canonical_entry_superseded_not_duplicated(tmp_path: Path) -> None:
    """Verify a stale copy of a canonical entry is superseded, not duplicated.

    The [project] version entry gained a regex anchor (so it cannot bleed into
    `[tool.nuitka]`'s file-/product-version keys). A downstream repo still
    carrying the old unanchored form must converge on the single canonical
    anchored entry rather than ending up with both, which would break the bump.
    """
    content = _bumpversion_pyproject(
        "[[tool.bumpversion.files]]\n"
        'filename = "./pyproject.toml"\n'
        "search = 'version = \"{current_version}\"'\n"
        "replace = 'version = \"{new_version}\"'\n"
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")

    result = init_config("bumpversion", pyproject)

    assert result is not None
    files = tomlrt.loads(result)["tool"]["bumpversion"]["files"]
    version_entries = [
        e for e in files if e.get("replace") == 'version = "{new_version}"'
    ]
    assert len(version_entries) == 1
    # The survivor is the canonical anchored form, not the stale unanchored one.
    assert version_entries[0].get("regex") is True
    assert version_entries[0]["search"].startswith("(?m)")


def test_local_entries_preserved_via_update(tmp_path: Path) -> None:
    """Verify local array entries survive ongoing sync via dict comparison."""
    content = _bumpversion_pyproject(
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

    bv = COMPONENTS_BY_NAME["bumpversion"]
    assert isinstance(bv, ToolConfigComponent)
    result = _sync_tool_config(content, bv)

    assert result is not None
    parsed = tomlrt.loads(result)
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
    content = _bumpversion_pyproject(
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

    bv = COMPONENTS_BY_NAME["bumpversion"]
    assert isinstance(bv, ToolConfigComponent)
    result = _sync_tool_config(content, bv)

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
    identifiers = tomlrt.loads(result)["tool"]["typos"]["default"]["extend-identifiers"]
    # Compared against the bundled template so the misspelled canonical keys
    # never appear as literals here, where `fix-typos` would "correct" them.
    assert _canonical_typos_identifiers().items() <= identifiers.items()


def test_typos_preserves_local_inline_table_keys(tmp_path: Path) -> None:
    """Local keys inside a shared inline table survive alongside canonical ones."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_TYPOS, encoding="UTF-8")

    result = init_config("typos", pyproject)

    assert result is not None
    default = tomlrt.loads(result)["tool"]["typos"]["default"]
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
    files = tomlrt.loads(result)["tool"]["typos"]["files"]
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
    identifiers = tomlrt.loads(result)["tool"]["typos"]["default"]["extend-identifiers"]
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


def test_typos_skips_svg_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A word split across two `<text>` runs survives a real `fix-typos` pass.

    A terminal capture renders each run of same-styled characters as one
    `<text>` element, so a word cut at a run boundary leaves a fragment the
    checker reads as a misspelling. Rewriting it in place doubles a letter and
    corrupts the picture, which `type.svg.check-file` prevents.

    The plain-text control proves the dictionary still carries the fragment, so
    the SVG half cannot pass for the wrong reason once typos drops the entry.
    """
    skip_unless_tool_runs("typos")

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_WITH_TYPOS, encoding="UTF-8")
    merged = init_config("typos", pyproject)
    assert merged is not None
    pyproject.write_text(merged, encoding="UTF-8")

    # Cut at runtime so the fragment never appears as a literal here, where
    # `fix-typos` would "correct" it.
    word = "Style"
    split = f"<text>{word[:-1]}</text><text>{word[-1]}</text>"
    joined = f"<text>{word}</text><text>{word[-1]}</text>"
    capture = tmp_path / "capture.svg"
    capture.write_text(split, encoding="UTF-8")
    control = tmp_path / "control.txt"
    control.write_text(split, encoding="UTF-8")

    monkeypatch.chdir(tmp_path)
    run_tool("typos", extra_args=("capture.svg", "control.txt"))

    assert capture.read_text(encoding="UTF-8") == split
    assert control.read_text(encoding="UTF-8") == joined


# --- Init exclusion tests ---


def test_init_default_excludes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify default exclude skips agents, labels, and skills."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Agents, labels, plugin, and skills are excluded by default.
    assert "labels.toml" not in created_set
    for _, rel_path in (
        (e.source, e.target) for e in COMPONENTS_BY_NAME["subagents"].files
    ):
        assert rel_path not in created_set
    for _, rel_path in (
        (e.source, e.target) for e in COMPONENTS_BY_NAME["skills"].files
    ):
        assert rel_path not in created_set

    # Other default components should still be created.
    assert "changelog.md" in created_set

    assert result.excluded == ["labels", "plugin", "skills", "subagents"]


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
    for _, rel_path in (
        (e.source, e.target) for e in COMPONENTS_BY_NAME["skills"].files
    ):
        assert rel_path not in created_set

    # Other default components should still be created.
    assert "changelog.md" in created_set

    assert result.excluded == ["labels", "plugin", "skills", "subagents"]


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


def test_init_scoped_component_excluded_existing_for_awesome_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An existing out-of-scope component file is flagged as excluded, not created."""
    action = tmp_path / ".github" / "actions" / "publish-pypi" / "action.yaml"
    action.parent.mkdir(parents=True, exist_ok=True)
    action.write_text("name: Publish\nruns:\n  using: composite\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path, repo_slug="user/awesome-billing")

    assert ".github/actions/publish-pypi/action.yaml" in result.excluded_existing


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

    ``repomatic init publish-pypi-action`` in an awesome repo should produce
    ``.github/actions/publish-pypi/action.yaml`` even though the component has
    ``scope=PACKAGE_ONLY`` and awesome repos build no distributable. Scope
    exclusions only apply during bare ``repomatic init`` (no explicit
    components).
    """
    monkeypatch.chdir(tmp_path)

    result = run_init(
        components=["publish-pypi-action"],
        output_dir=tmp_path,
        repo_slug="user/awesome-billing",
    )

    created_set = set(result.created)
    assert ".github/actions/publish-pypi/action.yaml" in created_set


def test_include_config_bypasses_scope_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Config ``include`` bypasses scope exclusions, like CLI explicit naming.

    ``publish-pypi-action`` is a ``BundledComponent`` with files, scoped
    ``PACKAGE_ONLY`` and ``init_default=INCLUDE``. In an awesome repo, which
    builds no distributable, it would normally be scope-excluded. With
    ``include``, the scope bypass falls through to file-level checks (the file
    has ``scope=ALL``, so nothing is excluded).
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.repomatic]\ninclude = ["publish-pypi-action"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(
        output_dir=tmp_path,
        repo_slug="user/awesome-billing",
    )

    assert ".github/actions/publish-pypi/action.yaml" in set(result.created)


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
    # Scoped component and workflow files are scope-excluded in awesome repos.
    assert ".github/actions/publish-pypi/action.yaml" not in created_set
    assert ".github/workflows/changelog.yaml" not in created_set
    assert ".github/workflows/debug.yaml" not in created_set
    assert ".github/workflows/release.yaml" not in created_set
    # Non-scoped workflows are still created.
    assert ".github/workflows/lint.yaml" in created_set


def _scoped_targets(scope: RepoScope) -> set[str]:
    """Collect every output path the registry gates behind *scope*.

    Covers both granularities: a component whose own ``scope`` matches (all of
    its files ride along) and an individual ``FileEntry`` that opts in.
    """
    targets: set[str] = set()
    for comp in COMPONENTS:
        comp_matches = comp.scope is scope
        if comp_matches and isinstance(comp, GeneratedComponent) and comp.target:
            targets.add(comp.target)
        for entry in comp.files:
            if comp_matches or entry.scope is scope:
                targets.add(entry.target)
    return targets


def test_virtual_project_init_drops_exactly_the_release_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A uv virtual project loses the release lane and keeps everything else.

    Conformance test for the ``PYTHON_ONLY``/``PACKAGE_ONLY`` split. Rather
    than naming the affected files (which would go stale the moment an entry
    moves between the two scopes), it inits the same repository twice, once as
    a distributable package and once as a virtual project, and asserts the
    difference is *exactly* the registry's ``PACKAGE_ONLY`` population.

    That pins both directions at once: nothing from the release lane survives
    into a repo that can never publish, and no ``PYTHON_ONLY`` entry gets
    dragged out with it. A virtual project still locks dependencies and runs
    tests, so over-narrowing is as much a bug as under-narrowing.
    """
    pep621 = '[project]\nname = "melon-stand"\nversion = "0.1.0"\n'
    created = {}
    for label, extra in (
        ("package", ""),
        ("virtual", "\n[tool.uv]\npackage = false\n"),
    ):
        target_dir = tmp_path / label
        target_dir.mkdir()
        (target_dir / "pyproject.toml").write_text(pep621 + extra, encoding="UTF-8")
        monkeypatch.chdir(target_dir)
        result = run_init(output_dir=target_dir, repo_slug="user/melon-stand")
        created[label] = set(result.created)

    package_only = _scoped_targets(RepoScope.PACKAGE_ONLY)
    # Guard against the enumeration silently going empty and passing by default.
    assert package_only

    assert created["package"] - created["virtual"] == package_only
    assert created["virtual"] - created["package"] == set()


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
        'exclude = ["labels/labels.toml"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    # `labels` is ephemeral, so only naming it explicitly writes it out. This
    # is the path `sync-labels` takes to stage definitions for labelmaker.
    result = run_init(output_dir=tmp_path, components=("labels",))

    assert "labels.toml" not in set(result.created)


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
    # Stage the label files the way the labeller jobs do, by naming the
    # ephemeral component explicitly.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)
    run_init(output_dir=tmp_path, components=("labels",))

    labels_toml = tmp_path / "labels.toml"
    assert labels_toml.exists()

    # A bare init leaves them out, so the staged copies read as excluded.
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
    assert ".claude/skills/awesome-triage" in result.excluded_existing
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
    assert ".claude/skills/awesome-triage" in result.excluded_existing


@pytest.mark.parametrize(
    ("component", "field", "default_dir", "leaf"),
    [
        pytest.param("skills", "skills_location", ".claude/skills", "papaya/SKILL.md"),
        pytest.param("subagents", "subagents_location", ".claude/agents", "papaya.md"),
    ],
)
@pytest.mark.parametrize(
    ("location", "expected_dir"),
    [
        pytest.param("./.claude/{leaf_dir}/", None, id="default"),
        pytest.param(".claude/{leaf_dir}/", None, id="default-no-dot-slash"),
        pytest.param("./custom/", "custom", id="custom"),
        pytest.param("custom/dir/", "custom/dir", id="custom-nested"),
        pytest.param("./.hidden/{leaf_dir}/", ".hidden/{leaf_dir}", id="hidden-dir"),
    ],
)
def test_resolve_target(component, field, default_dir, leaf, location, expected_dir):
    """A declared target is rebased onto the configured location.

    `removeprefix` (not `strip`) normalizes the `./` the config default carries,
    so a location that is itself a hidden directory keeps its leading dot.
    """
    leaf_dir = default_dir.rsplit("/", 1)[-1]
    location = location.format(leaf_dir=leaf_dir)
    expected_prefix = (
        default_dir if expected_dir is None else expected_dir.format(leaf_dir=leaf_dir)
    )
    config = Config(**{field: location})
    comp = COMPONENTS_BY_NAME[component]
    assert (
        comp.resolve_target(f"{default_dir}/{leaf}", config)
        == f"{expected_prefix}/{leaf}"
    )


@pytest.mark.parametrize("component", ["skills", "subagents"])
def test_resolve_target_leaves_foreign_paths_alone(component):
    """A target outside the component's default location passes through."""
    comp = COMPONENTS_BY_NAME[component]
    config = Config(skills_location="./custom/", subagents_location="./custom/")
    assert comp.resolve_target("other/path.md", config) == "other/path.md"


@pytest.mark.parametrize("component", ["skills", "subagents", "workflows"])
def test_resolve_target_without_config_is_noop(component):
    """With no config there is nothing to rebase onto."""
    comp = COMPONENTS_BY_NAME[component]
    target = comp.files[0].target
    assert comp.resolve_target(target, None) == target


def test_resolve_target_ignores_components_with_fixed_location():
    """A component with no configurable location never rebases.

    `.github/workflows/` is GitHub's path, not one the user may move, so
    `workflows` declares no `location_field` and its targets are literal.
    """
    comp = COMPONENTS_BY_NAME["workflows"]
    config = Config(skills_location="./custom/", subagents_location="./custom/")
    assert comp.resolve_target(".github/workflows/autofix.yaml", config) == (
        ".github/workflows/autofix.yaml"
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


def test_init_agents_custom_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify agents are written to a custom location when configured."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n'
        "[tool.repomatic]\n"
        'include = ["subagents"]\n'
        'subagents.location = "./custom/agents/"\n',
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
        'include = ["subagents"]\n'
        'subagents.location = "./custom/agents/"\n',
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
        'include = ["subagents"]\n'
        'exclude = ["subagents/grunt-qa"]\n'
        'subagents.location = "./custom/agents/"\n',
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
    assert "custom/skills/awesome-triage" in result.excluded_existing


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


# --- Removed-asset (tombstone) tests ---


def test_removed_assets_sorted() -> None:
    """REMOVED_ASSETS is ordered by (component, target)."""
    keys = [(a.component, a.target) for a in REMOVED_ASSETS]
    assert keys == sorted(keys)


def test_removed_assets_no_collision_with_live_registry() -> None:
    """No tombstone may point at a path a live component still ships.

    Guards against re-adding an asset without dropping its tombstone, which
    would make ``init`` prune a file it just wrote.
    """
    live = {entry.target for comp in COMPONENTS for entry in comp.files}
    for asset in REMOVED_ASSETS:
        assert asset.target not in live, (
            f"{asset.target!r} is both tombstoned and live in the registry"
        )


@pytest.mark.parametrize("asset", REMOVED_ASSETS, ids=lambda a: a.target)
def test_removed_asset_metadata_valid(asset: RemovedAsset) -> None:
    """Each tombstone has a valid gate, a bare version, and a known component."""
    if asset.component == "workflows":
        # Fingerprint-gated by the `uses:` line, never content-hashed.
        assert asset.hashes == (), f"{asset.target} should be fingerprint-gated"
    else:
        # Content-gated: at least one 64-hex shipped-content hash.
        assert asset.hashes, f"{asset.target} has no shipped hashes"
        for digest in asset.hashes:
            assert re.fullmatch(r"[0-9a-f]{64}", digest), digest
    assert not asset.removed_in.startswith("v"), asset.removed_in
    assert Version(asset.removed_in) <= Version(__version__), asset.removed_in
    # The component name drives the `_resolve_target` location override, so a
    # typo would silently skip it. A dropped *whole* component (`codecov`) has
    # no live entry to name, and needs none: its target is a `.github/` path
    # GitHub fixes and no `[tool.repomatic] *.location` can relocate.
    assert asset.component in ALL_COMPONENTS or asset.target.startswith(".github/"), (
        f"{asset.component!r} is neither a live component nor a fixed .github/ path"
    )
    # A skill ships as a folder, so its tombstone names that folder: without it
    # the folder outlives its SKILL.md and no later init can see it again. Every
    # other asset is a lone file in a directory it shares, which must never be
    # swept.
    if asset.component == "skills":
        assert asset.owned_dir == asset.target.removesuffix(f"/{SKILL_FILENAME}"), (
            f"{asset.target} does not declare the folder it owns"
        )
    else:
        assert not asset.owned_dir, f"{asset.target} is not shipped as a folder"


def test_detect_removed_assets_classifies_by_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_detect_removed_assets`` sorts orphans into prunable vs review by hash."""
    rel = ".claude/skills/gone/SKILL.md"
    content = "Old skill body.\n"
    digest = hashlib.sha256(content.encode("UTF-8")).hexdigest()
    asset = RemovedAsset("skills", rel, "1.0.0", (digest,), successor="moved on")
    monkeypatch.setattr("repomatic.init_project.REMOVED_ASSETS", (asset,))

    target = tmp_path / rel
    target.parent.mkdir(parents=True)

    # Absent on disk: neither list.
    assert _detect_removed_assets(tmp_path, None) == ([], [])

    # Byte-identical to last-shipped: prunable.
    target.write_text(content, encoding="UTF-8")
    assert _detect_removed_assets(tmp_path, None) == ([(rel, "moved on")], [])

    # Trailing-whitespace churn is normalized away: still prunable.
    target.write_text(content.rstrip() + "\n\n\n", encoding="UTF-8")
    assert _detect_removed_assets(tmp_path, None) == ([(rel, "moved on")], [])

    # Real content change: review, never prunable.
    target.write_text(content + "local edit\n", encoding="UTF-8")
    assert _detect_removed_assets(tmp_path, None) == ([], [(rel, "moved on")])


def test_detect_removed_assets_prunes_an_empty_owned_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill folder outliving its ``SKILL.md`` is an orphan nothing else sees."""
    skill_dir = ".claude/skills/gone"
    asset = RemovedAsset(
        "skills",
        f"{skill_dir}/{SKILL_FILENAME}",
        "1.0.0",
        ("0" * 64,),
        owned_dir=skill_dir,
        successor="moved on",
    )
    monkeypatch.setattr("repomatic.init_project.REMOVED_ASSETS", (asset,))

    # Neither the file nor its folder: nothing to report.
    assert _detect_removed_assets(tmp_path, None) == ([], [])

    # The folder alone, empty: prunable, and reported as the folder.
    (tmp_path / skill_dir).mkdir(parents=True)
    assert _detect_removed_assets(tmp_path, None) == ([(skill_dir, "moved on")], [])

    # Still carrying files: whatever they are, repomatic did not write them.
    (tmp_path / skill_dir / "scripts").mkdir()
    assert _detect_removed_assets(tmp_path, None) == ([], [])


def _seed_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> Path:
    """Seed a minimal repo with a dropped-asset orphan and chdir into it."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="UTF-8"
    )
    orphan = tmp_path / ".claude/skills/gone/SKILL.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text(content, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    return orphan


def test_init_cli_prunes_unmodified_removed_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``init`` deletes an unmodified orphan and its emptied parent dir."""
    content = "Old skill body.\n"
    digest = hashlib.sha256(content.encode("UTF-8")).hexdigest()
    monkeypatch.setattr(
        "repomatic.init_project.REMOVED_ASSETS",
        (RemovedAsset("skills", ".claude/skills/gone/SKILL.md", "1.0.0", (digest,)),),
    )
    orphan = _seed_orphan(tmp_path, monkeypatch, content)

    cli_result = CliRunner().invoke(repomatic, ["init", "--output-dir", str(tmp_path)])

    assert cli_result.exit_code == 0, cli_result.output
    assert not orphan.exists()
    assert not orphan.parent.exists()
    assert "Pruned" in cli_result.output


def test_init_cli_keeps_modified_removed_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locally modified orphan is reported for review, never auto-deleted."""
    content = "Old skill body.\n"
    digest = hashlib.sha256(content.encode("UTF-8")).hexdigest()
    monkeypatch.setattr(
        "repomatic.init_project.REMOVED_ASSETS",
        (RemovedAsset("skills", ".claude/skills/gone/SKILL.md", "1.0.0", (digest,)),),
    )
    orphan = _seed_orphan(tmp_path, monkeypatch, content + "local edit\n")

    cli_result = CliRunner().invoke(repomatic, ["init", "--output-dir", str(tmp_path)])

    assert cli_result.exit_code == 0, cli_result.output
    assert orphan.exists()
    assert "Review manually" in cli_result.output


def test_init_cli_keep_removed_preserves_unmodified_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--keep-removed`` reports an unmodified orphan but does not delete it."""
    content = "Old skill body.\n"
    digest = hashlib.sha256(content.encode("UTF-8")).hexdigest()
    monkeypatch.setattr(
        "repomatic.init_project.REMOVED_ASSETS",
        (RemovedAsset("skills", ".claude/skills/gone/SKILL.md", "1.0.0", (digest,)),),
    )
    orphan = _seed_orphan(tmp_path, monkeypatch, content)

    cli_result = CliRunner().invoke(
        repomatic, ["init", "--keep-removed", "--output-dir", str(tmp_path)]
    )

    assert cli_result.exit_code == 0, cli_result.output
    assert orphan.exists()
    assert "--keep-removed" in cli_result.output


def test_detect_removed_assets_matches_any_shipped_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy matching any released revision (not just the last) is prunable."""
    rel = ".claude/skills/gone/SKILL.md"
    old_content = "Version 1 body.\n"
    new_content = "Version 2 body.\n"
    hashes = tuple(
        hashlib.sha256(c.encode("UTF-8")).hexdigest()
        for c in (old_content, new_content)
    )
    monkeypatch.setattr(
        "repomatic.init_project.REMOVED_ASSETS",
        (RemovedAsset("skills", rel, "1.0.0", hashes),),
    )
    target = tmp_path / rel
    target.parent.mkdir(parents=True)

    # A downstream repo still on the older shipped revision is recognized.
    target.write_text(old_content, encoding="UTF-8")
    assert _detect_removed_assets(tmp_path, None) == ([(rel, "")], [])

    # Content that never shipped is treated as a local modification.
    target.write_text("never shipped\n", encoding="UTF-8")
    assert _detect_removed_assets(tmp_path, None) == ([], [(rel, "")])


def test_init_cli_delete_removed_modified_forces_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--delete-removed-modified`` deletes a locally modified orphan."""
    content = "Old skill body.\n"
    digest = hashlib.sha256(content.encode("UTF-8")).hexdigest()
    monkeypatch.setattr(
        "repomatic.init_project.REMOVED_ASSETS",
        (RemovedAsset("skills", ".claude/skills/gone/SKILL.md", "1.0.0", (digest,)),),
    )
    orphan = _seed_orphan(tmp_path, monkeypatch, content + "local edit\n")

    cli_result = CliRunner().invoke(
        repomatic, ["init", "--delete-removed-modified", "--output-dir", str(tmp_path)]
    )

    assert cli_result.exit_code == 0, cli_result.output
    assert not orphan.exists()
    assert "Force-deleted" in cli_result.output


def test_init_cli_keep_removed_and_force_are_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--keep-removed`` and ``--delete-removed-modified`` cannot combine."""
    monkeypatch.chdir(tmp_path)
    cli_result = CliRunner().invoke(
        repomatic,
        [
            "init",
            "--keep-removed",
            "--delete-removed-modified",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert cli_result.exit_code != 0
    assert "mutually exclusive" in cli_result.output


# A removed reusable workflow seeded in REMOVED_ASSETS, used by the
# fingerprint-gated workflow tests below.
_REMOVED_WORKFLOW = ".github/workflows/label-sponsors.yaml"


def _thin_caller(slug: str = "kdeldycke/repomatic", extra: str = "") -> str:
    """A downstream thin-caller for the removed `label-sponsors` workflow."""
    return (
        "---\n"
        "name: 🏷️ Label sponsors\n"
        '"on":\n'
        "  pull_request:\n\n"
        "jobs:\n\n"
        "  label-sponsors:\n"
        f"    uses: {slug}/.github/workflows/label-sponsors.yaml@v4.24.6\n"
        "    secrets:\n"
        "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
        f"{extra}"
    )


def _write_removed_workflow(tmp_path: Path, content: str) -> Path:
    wf = tmp_path / _REMOVED_WORKFLOW
    wf.parent.mkdir(parents=True)
    wf.write_text(content, encoding="UTF-8")
    return wf


@pytest.mark.parametrize(
    "slug", ["kdeldycke/repomatic", "kdeldycke/repokit", "kdeldycke/workflows"]
)
def test_detect_prunes_thin_caller_any_slug(tmp_path: Path, slug: str) -> None:
    """A pure thin-caller for a removed workflow is prunable, whatever the era slug."""
    _write_removed_workflow(tmp_path, _thin_caller(slug))
    prunable, review = _detect_removed_assets(tmp_path, None)
    assert (_REMOVED_WORKFLOW, "merged into labels.yaml") in prunable
    assert review == []


def test_detect_reviews_customized_thin_caller(tmp_path: Path) -> None:
    """A thin-caller the user extended with extra jobs is reported, never pruned."""
    extra = "  my-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
    _write_removed_workflow(tmp_path, _thin_caller(extra=extra))
    prunable, review = _detect_removed_assets(tmp_path, None)
    assert (_REMOVED_WORKFLOW, "merged into labels.yaml") in review
    assert prunable == []


def test_detect_skips_unrelated_workflow(tmp_path: Path) -> None:
    """A user's own workflow that merely shares the name is left untouched."""
    _write_removed_workflow(
        tmp_path,
        '---\nname: Mine\n"on": pull_request\njobs:\n'
        "  labeller:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo mine\n",
    )
    prunable, review = _detect_removed_assets(tmp_path, None)
    assert prunable == []
    assert review == []


def test_init_cli_prunes_orphaned_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare init deletes a pure thin-caller for a removed reusable workflow."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="UTF-8"
    )
    wf = _write_removed_workflow(tmp_path, _thin_caller())
    monkeypatch.chdir(tmp_path)

    cli_result = CliRunner().invoke(repomatic, ["init", "--output-dir", str(tmp_path)])

    assert cli_result.exit_code == 0, cli_result.output
    assert not wf.exists()
    assert "Pruned" in cli_result.output


@pytest.mark.once
def test_removed_reusable_workflows_are_tombstoned() -> None:
    """A reusable workflow dropped since the last release must be tombstoned.

    A workflow with a ``workflow_call`` trigger generates downstream
    thin-callers; removing it without a ``RemovedAsset`` leaves orphaned
    thin-callers. Skips when git history or release tags are unavailable.
    """
    repo_root = Path(__file__).resolve().parents[1]

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="UTF-8",
            cwd=repo_root,
            check=False,
        )

    tags_out = git("tag", "--list", "v*")
    if tags_out.returncode != 0 or not tags_out.stdout.split():
        pytest.skip("git tags unavailable")

    def version_key(tag: str) -> Version:
        try:
            return Version(tag.lstrip("v"))
        except InvalidVersion:
            return Version("0")

    prev = max(tags_out.stdout.split(), key=version_key)
    tree = git("ls-tree", "-r", prev, ".github/workflows/")
    if tree.returncode != 0:
        pytest.skip(f"cannot read tree at {prev}")

    old_reusable = set()
    for line in tree.stdout.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        m = re.search(r"\.github/workflows/([^/]+\.ya?ml)$", path)
        if m and re.search(
            r"^\s*workflow_call:", git("cat-file", "-p", parts[2]).stdout, re.MULTILINE
        ):
            old_reusable.add(m.group(1))

    # "Currently shipped" reusable workflows: the registry's downstream file_ids
    # plus the release engine lanes. The lanes (RELEASE_ENGINE_WORKFLOWS) are
    # referenced by the generated release.yaml via `uses:` rather than carried as
    # registry file_ids (whose file_id is `release.yaml`), so without them they
    # would look "removed" the moment a release tag includes them.
    current_reusable = set(REUSABLE_WORKFLOWS) | set(RELEASE_ENGINE_WORKFLOWS)

    tombstoned = {
        a.target.rsplit("/", 1)[-1]
        for a in REMOVED_ASSETS
        if a.component == "workflows"
    }
    for name in sorted(old_reusable - current_reusable):
        assert name in tombstoned, (
            f"reusable workflow {name!r} present in {prev} but removed without a "
            f"RemovedAsset tombstone; add one to REMOVED_ASSETS"
        )


@pytest.mark.once
def test_removed_data_assets_are_tombstoned() -> None:
    """A skill/agent data file dropped since the last release must be tombstoned.

    Compares the bundled skill/agent data files at the most recent release tag
    against the current tree. A file present then but gone now is a removed
    asset that must have a matching ``RemovedAsset`` entry, or ``init`` would
    leave orphans in downstream repos. Skips when git history or release tags
    are unavailable (shallow clone, no tags).
    """
    repo_root = Path(__file__).resolve().parents[1]

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="UTF-8",
            cwd=repo_root,
            check=False,
        )

    tags_out = git("tag", "--list", "v*")
    if tags_out.returncode != 0:
        pytest.skip("git unavailable")
    tags = [t for t in tags_out.stdout.split() if t]
    if not tags:
        pytest.skip("no release tags available")

    def version_key(tag: str) -> Version:
        try:
            return Version(tag.lstrip("v"))
        except InvalidVersion:
            return Version("0")

    prev = max(tags, key=version_key)
    old_tree = git("ls-tree", "-r", "--name-only", prev, "repomatic/data/")
    if old_tree.returncode != 0:
        pytest.skip(f"cannot read tree at {prev}")

    asset_re = re.compile(r"repomatic/data/((?:skill|agent)-.+\.md)$")
    old_sources = {
        m.group(1)
        for line in old_tree.stdout.splitlines()
        if (m := asset_re.match(line))
    }
    data_dir = repo_root / "repomatic" / "data"
    current_sources = {p.name for p in data_dir.glob("skill-*.md")} | {
        p.name for p in data_dir.glob("agent-*.md")
    }

    # What init currently writes downstream, which is what decides whether a
    # dropped bundled source actually orphans anything.
    shipped_targets = {
        f"{entry.target}/{SKILL_FILENAME}" if entry.tree else entry.target
        for name in ("skills", "subagents")
        for entry in COMPONENTS_BY_NAME[name].files
    }

    tombstoned = {a.target for a in REMOVED_ASSETS}
    for source in sorted(old_sources - current_sources):
        if source.startswith("skill-"):
            target = _skill_target(source[len("skill-") : -len(".md")])
        else:
            target = _subagent_target(source[len("agent-") : -len(".md")])
        # Relocating a bundled source (a skill moving to its folder layout)
        # leaves no orphan as long as the downstream path is unchanged.
        if target in shipped_targets:
            continue
        assert target in tombstoned, (
            f"{source!r} shipped in {prev} but was removed without a RemovedAsset "
            f"tombstone (expected target {target!r}); add one to REMOVED_ASSETS"
        )


@pytest.mark.once
def test_shrinking_component_file_lists_are_tombstoned() -> None:
    """A target a component stopped shipping must carry a tombstone.

    The sibling tests above compare *sources* under `repomatic/data/`, which
    only catches a bundled file being deleted. A component can orphan a
    downstream file without deleting anything: dropping a `FileEntry` from its
    `files` tuple stops shipping that target while the source lives on (or
    goes, independently). `labels` did exactly that, shedding the two
    `.github/labeller-*.yaml` entries while `labels.toml` stayed.

    Nothing catches that on its own, because `_detect_removed_assets` walks
    `REMOVED_ASSETS` alone: it never diffs a component's current tuple against
    what earlier versions wrote, so a shrunk tuple leaves a downstream orphan
    no later `init` will ever look at.

    Comparing target sets across the previous release closes it. Skips when
    git history or release tags are unavailable (shallow clone, no tags).

    ```{note}
    One-sided by construction: the previous registry is read as text, so the
    "shipped then" set covers only `FileEntry` literals carrying an explicit
    target, while "shipped now" is the fully built registry. A component whose
    entries are generated by a helper is therefore invisible on the old side.
    That direction is the safe one — it can only miss a drop, never invent
    one — but it does mean a green run here is not proof that no component
    shrank, only that no literal-target one did.
    ```
    """
    repo_root = Path(__file__).resolve().parents[1]

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="UTF-8",
            cwd=repo_root,
            check=False,
        )

    tags_out = git("tag", "--list", "v*")
    if tags_out.returncode != 0:
        pytest.skip("git unavailable")
    tags = [t for t in tags_out.stdout.split() if t]
    if not tags:
        pytest.skip("no release tags available")

    def version_key(tag: str) -> Version:
        try:
            return Version(tag.lstrip("v"))
        except InvalidVersion:
            return Version("0")

    prev = max(tags, key=version_key)
    old_source = git("show", f"{prev}:repomatic/registry.py")
    if old_source.returncode != 0:
        pytest.skip(f"cannot read registry at {prev}")

    # Read the previous registry as text, not by importing it: an older module
    # need not import under the current dependency set, and every target is a
    # literal in the source either way.
    old_targets = set(
        re.findall(r'FileEntry\(\s*"[^"]+",\s*"([^"]+)"', old_source.stdout)
    )
    current_targets = {entry.target for comp in COMPONENTS for entry in comp.files}
    tombstoned = {a.target for a in REMOVED_ASSETS}

    for target in sorted(old_targets - current_targets - tombstoned):
        pytest.fail(
            f"{target!r} was shipped by a component in {prev} and no longer is, "
            f"without a RemovedAsset tombstone. Downstream repos that committed "
            f"it keep it forever: add one to REMOVED_ASSETS."
        )


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
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["subagents"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    created_set = set(result.created)
    # Agents included via include; labels, skills still excluded by default.
    for _, rel_path in (
        (e.source, e.target) for e in COMPONENTS_BY_NAME["subagents"].files
    ):
        assert rel_path in created_set
    for _, rel_path in (
        (e.source, e.target) for e in COMPONENTS_BY_NAME["skills"].files
    ):
        assert rel_path not in created_set
    assert "labels.toml" not in created_set
    assert result.excluded == ["labels", "plugin", "skills"]


def test_init_include_cannot_materialize_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Verify `include` cannot opt a bare init into an ephemeral component."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n\n[tool.repomatic]\ninclude = ["labels"]\n',
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)

    result = run_init(output_dir=tmp_path)

    assert "labels.toml" not in set(result.created)
    assert not (tmp_path / "labels.toml").exists()
    assert "labels" in result.excluded
    # The override is refused out loud, naming the way to write them out.
    assert "ephemeral component 'labels'" in caplog.text
    assert "repomatic init labels" in caplog.text


def test_init_explicit_selection_materializes_ephemeral(tmp_path: Path):
    """Verify naming an ephemeral component explicitly still writes its files."""
    result = run_init(output_dir=tmp_path, components=("labels",))

    assert set(result.created) == {"labels.toml"}


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
    for _, rel_path in (
        (e.source, e.target) for e in COMPONENTS_BY_NAME["skills"].files
    ):
        assert rel_path not in created_set
    # User exclude is additive.
    assert ".github/workflows/debug.yaml" not in created_set
    assert "labels" in result.excluded
    assert "skills" in result.excluded
    assert "workflows/debug.yaml" in result.excluded


# --- Data file registry and exclude validation tests ---


def test_all_data_files_registered_in_exportable_files() -> None:
    """Every non-infrastructure file in data/ must appear in EXPORTABLE_FILES."""
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


def test_valid_file_ids_cover_all_multi_file_components() -> None:
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
    for entry in COMPONENTS_BY_NAME["workflows"].files:
        assert entry.target.startswith(".github/workflows/"), (
            f"Workflow entry {entry.file_id!r} targets {entry.target!r},"
            " expected .github/workflows/ prefix"
        )


def test_workflow_sources_are_yaml() -> None:
    """All workflow source files must be .yaml."""
    for entry in COMPONENTS_BY_NAME["workflows"].files:
        assert entry.source.endswith(".yaml"), (
            f"Workflow entry {entry.file_id!r} source {entry.source!r}"
            " is not a .yaml file"
        )


def test_skill_files_target_skill_dir() -> None:
    """All skill entries must target the .claude/skills/{id} folder itself."""
    for entry in COMPONENTS_BY_NAME["skills"].files:
        assert entry.tree, f"Skill entry {entry.file_id!r} is not a folder entry"
        assert entry.target == _skill_dir(entry.file_id), (
            f"Skill entry {entry.file_id!r} targets {entry.target!r},"
            f" expected {_skill_dir(entry.file_id)!r}"
        )


def test_skill_sources_follow_naming_convention() -> None:
    """Skill sources must be the bundled skills/{id} folder."""
    for entry in COMPONENTS_BY_NAME["skills"].files:
        expected_source = _skill_source(entry.file_id)
        assert entry.source == expected_source, (
            f"Skill {entry.file_id!r}: source is {entry.source!r},"
            f" expected {expected_source!r}"
        )


def test_skill_file_id_matches_target_dir() -> None:
    """Skill file_id must match the directory name in the target path."""
    for entry in COMPONENTS_BY_NAME["skills"].files:
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


def test_every_config_key_gate_actually_reaches_its_field() -> None:
    """Every declared `config_key` must move its entry when the user flips it.

    A gate naming a key `Config` cannot resolve does not fail: it silently
    falls through to `config_default`, which reads as a feature switched off
    rather than as a gate wired wrong. `metrics.sync` shipped that way, because
    the resolver flattened every dotted key to one attribute name and could
    not reach a key living on a nested schema.

    Written against the whole registry rather than against that one entry: the
    fault is a property of how a gate is spelled, so any future entry naming a
    nested key would repeat it.
    """
    gates = [
        (f"{comp.name}", comp.config_key, comp.config_default)
        for comp in COMPONENTS
        if comp.config_key
    ] + [
        (f"{comp.name}/{entry.file_id}", entry.config_key, entry.config_default)
        for comp in COMPONENTS
        for entry in comp.files
        if entry.config_key
    ]
    assert gates, "no config-gated entry left to check"

    for label, key, default in gates:
        # Build the `[tool.repomatic]` table the user would write, nesting the
        # dotted key exactly as TOML would, and set it to the opposite of the
        # entry's assumed default.
        flipped = not default
        table: dict = {}
        node = table
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = flipped
        config = load_repomatic_config({"tool": {"repomatic": table}})

        entries = [e for c in COMPONENTS for e in c.files if e.config_key == key]
        subject = entries[0] if entries else COMPONENTS_BY_NAME[label]
        assert subject.is_enabled(config) is flipped, (
            f"{label} declares config_key {key!r}, but setting it to {flipped} "
            "left the gate unmoved: the key does not reach a Config field."
        )


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


@pytest.mark.parametrize(
    "entry",
    [
        "workflows/debug.yaml",
        "workflows/tests.yaml",
        "workflows/autofix.yaml",
        "skills/repomatic-audit",
        "labels/labels.toml",
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
    total_skills = len(COMPONENTS_BY_NAME["skills"].files)
    assert len(result.created) == total_skills


def test_init_mixed_bare_and_qualified(tmp_path: Path):
    """Bare + qualified for different components work independently."""
    result = run_init(
        output_dir=tmp_path,
        components=("labels", "skills/repomatic-topics"),
    )
    created_set = set(result.created)
    assert created_set == {
        "labels.toml",
        ".claude/skills/repomatic-topics/SKILL.md",
    }


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


# --- tomlrt contract tests ---
#
# These tests guard the assumptions `_update_tool_config` makes about its
# underlying TOML library. The tomlrt rewrite dropped a regex-based whitespace
# normalization stack and several inline-table workarounds that the previous
# tomlkit-based implementation needed; the guards below assert the library now
# meets those invariants natively, so a future regression cannot silently
# corrupt a downstream pyproject.toml.

_TOOL_CONFIG_COMPONENTS = [c for c in COMPONENTS if isinstance(c, ToolConfigComponent)]


@pytest.mark.parametrize("comp", _TOOL_CONFIG_COMPONENTS, ids=lambda c: c.name)
def test_update_tool_config_output_contract(comp: ToolConfigComponent) -> None:
    r"""Syncing any tool-config component yields well-formed, well-spaced TOML.

    Three invariants the production path has no other guard for, checked on one
    sync per component:

    - Non-empty output. tomlrt 1.7.3, 1.7.4, and the main-branch SHA briefly
      pinned in `pyproject.toml` each shipped fixes for a shape where
      `tomlrt.dumps` returned an empty string with no exception.
    - Section spacing. The previous tomlkit implementation enforced this with a
      four-step regex normalization on the dumped string; the tomlrt rewrite
      dropped that stack and trusts the library, so a regression reintroducing
      ``]\n[`` collisions or triple-blank gaps must fail here.
    - A terminal newline. When the template's prefix-strip drops the source's
      trailing newline, the last KV slot loses its EOL trivia and every
      regenerated PR diff grows a ``\ No newline at end of file``.
    """
    tool_name = comp.tool_section.removeprefix("tool.")
    seed = (
        '[project]\nname = "fixture"\n\n'
        f"[{comp.tool_section}]\n"
        'local_only_key = "preserved"\n'
    )

    result = _sync_tool_config(seed, comp)

    assert result is not None, "expected the sync to modify the seed"
    assert result.strip(), "tomlrt produced an empty document"
    parsed = tomlrt.loads(result)
    assert tool_name in parsed["tool"]
    assert parsed["tool"][tool_name].get("local_only_key") == "preserved"

    no_blank = re.search(r"[^\n]\n\[(?!\[)", result)
    assert no_blank is None, (
        f"section header without preceding blank line at offset "
        f"{no_blank.start() if no_blank else -1} in {comp.name} output"
    )
    assert "\n\n\n" not in result, f"triple blank line in {comp.name} output"
    assert result.endswith("\n"), (
        f"{comp.name}: synced output missing terminal newline; "
        f"last 60 chars: {result[-60:]!r}"
    )


def test_tomlrt_aot_only_section_overwrite_invariant() -> None:
    """A populate-then-overwrite of an AoT-only section preserves the document.

    Mirrors the ``_graft_local_additions`` + cross-doc assign path from
    ``_update_tool_config``: parse a pyproject with a ``[project]``
    preamble followed by an AoT-only target section, build a fresh
    detached section with ``Table.section()`` from an AoT-only template,
    populate it with the existing AoT entries, then assign the new
    section back into the document.

    Earlier tomlrt releases silently dropped the entire document body in
    this configuration: ``tomlrt.dumps(doc)`` returned an empty string
    with no exception. The 1.7.5 changelog entry "Overwriting a key with
    a body-less section (one whose only child is an array-of-tables) no
    longer wipes the whole document on dump" fixed it; this test pins
    the contract so a future tomlrt bump that regresses it fails CI
    instead of silently corrupting downstream ``pyproject.toml`` files.
    No bundled ``ToolConfigComponent`` ships an AoT-only template today,
    so a regression would not otherwise surface through the ``sync-*``
    autofix jobs until a user defined one.
    """
    src = '[project]\nname = "demo"\n\n[[tool.example.items]]\nkey = "existing"\n'
    doc = tomlrt.loads(src)
    new_section = tomlrt.Table.section(tomlrt.loads('[[items]]\nkey = "template"\n'))
    for entry in doc["tool"]["example"]["items"]:
        new_section["items"].append(entry)
    doc["tool"]["example"] = new_section

    result = tomlrt.dumps(doc)
    assert result.strip(), "tomlrt produced an empty dump"
