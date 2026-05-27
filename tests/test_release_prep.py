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

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from repomatic.release_prep import ReleasePrep


@pytest.fixture
def temp_changelog(tmp_path: Path) -> Path:
    """Create a temporary changelog file."""
    changelog = tmp_path / "changelog.md"
    changelog.write_text(
        dedent("""\
            # Changelog

            ## [1.2.3 (unreleased)](https://github.com/user/repo/compare/v1.2.2...main)

            > [!IMPORTANT]
            > This version is not released yet and is under active development.

            - Add new feature.
            - Fix bug.

            ## [1.2.2 (2024-01-15)](https://github.com/user/repo/compare/v1.2.1...v1.2.2)

            - Previous release.
            """),
        encoding="UTF-8",
    )
    return changelog


@pytest.fixture
def temp_citation(tmp_path: Path) -> Path:
    """Create a temporary citation file."""
    citation = tmp_path / "citation.cff"
    citation.write_text(
        dedent("""\
            cff-version: 1.2.0
            title: My Project
            version: 1.2.3
            date-released: 2024-01-01
            authors:
              - name: Test Author
            """),
        encoding="UTF-8",
    )
    return citation


@pytest.fixture
def temp_workflows(tmp_path: Path) -> Path:
    """Create a temporary workflows directory with sample files."""
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)

    (workflow_dir / "release.yaml").write_text(
        dedent("""\
            name: Release
            on: push
            jobs:
              build:
                uses: kdeldycke/repomatic/main/.github/workflows/release.yaml
            """),
        encoding="UTF-8",
    )

    (workflow_dir / "lint.yaml").write_text(
        dedent("""\
            name: Lint
            on: push
            jobs:
              lint:
                uses: kdeldycke/repomatic/main/.github/workflows/lint.yaml
            """),
        encoding="UTF-8",
    )

    return workflow_dir


@pytest.fixture
def temp_workflows_with_actions(tmp_path: Path) -> Path:
    """Create workflows referencing a discoverable composite action.

    The action freeze/unfreeze logic enumerates `.github/actions/*/action.y*ml`
    to decide which action names to rewrite, so the fixture materializes a
    dummy `pr-metadata/action.yaml` alongside the workflow that references it.
    """
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    actions_dir = tmp_path / ".github" / "actions" / "pr-metadata"
    actions_dir.mkdir(parents=True)
    (actions_dir / "action.yaml").write_text(
        "name: pr-metadata\nruns:\n  using: composite\n  steps: []\n",
        encoding="UTF-8",
    )

    (workflow_dir / "autofix.yaml").write_text(
        dedent("""\
            name: Autofix
            on: push
            jobs:
              format:
                steps:
                  - id: pr-metadata
                    uses: kdeldycke/repomatic/.github/actions/pr-metadata@main
                  - uses: peter-evans/create-pull-request@v8
                    with:
                      body: ${{ steps.pr-metadata.outputs.body }}
            """),
        encoding="UTF-8",
    )

    return workflow_dir


@pytest.fixture
def temp_workflows_with_cli(tmp_path: Path) -> Path:
    """Create workflows with ``--from . repomatic`` CLI invocations."""
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)

    (workflow_dir / "lint.yaml").write_text(
        dedent("""\
            name: Lint
            on: push
            jobs:
              metadata:
                steps:
                  - run: >
                      uvx --no-progress --from . repomatic
                      metadata --output "$GITHUB_OUTPUT"
              lint:
                steps:
                  - run: >
                      uvx --no-progress --from . repomatic
                      lint-repo --repo-name "test"
            """),
        encoding="UTF-8",
    )

    (workflow_dir / "autofix.yaml").write_text(
        dedent("""\
            name: Autofix
            on: push
            jobs:
              format:
                steps:
                  - run: >
                      uvx --no-progress --from . repomatic
                      metadata --output "$GITHUB_OUTPUT"
                  - run: uvx --no-progress --from . repomatic pr-body
            """),
        encoding="UTF-8",
    )

    return workflow_dir


@pytest.fixture
def temp_pyproject(tmp_path: Path) -> Path:
    """Create a temporary pyproject.toml with bumpversion config."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        dedent("""\
            [project]
            name = "test-project"
            version = "1.2.3"

            [tool.bumpversion]
            current_version = "1.2.3"
            """),
        encoding="UTF-8",
    )
    return pyproject


def test_current_version_from_pyproject(
    tmp_path: Path, temp_pyproject: Path, monkeypatch
) -> None:
    """Test that current version is read from pyproject.toml."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep()

    assert prep.current_version == "1.2.3"


def test_freeze_action_reference(
    tmp_path: Path,
    temp_workflows_with_actions: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that composite action references are frozen to versioned tag."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(workflow_dir=temp_workflows_with_actions)
    count = prep.freeze_workflow_urls()

    assert count == 1
    content = (temp_workflows_with_actions / "autofix.yaml").read_text(encoding="UTF-8")
    assert "@main" not in content
    assert "@v1.2.3" in content
    assert "kdeldycke/repomatic/.github/actions/pr-metadata@v1.2.3" in content


def test_composite_action_names_enumerates_actions_dir(tmp_path: Path) -> None:
    """`composite_action_names` discovers every action.y(a)ml directory.

    Conformance test: a freshly-created action under `.github/actions/`
    must appear in the discovery list without code changes elsewhere.
    """
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    actions_dir = tmp_path / ".github" / "actions"
    for name in ("alpha", "bravo", "charlie"):
        action_dir = actions_dir / name
        action_dir.mkdir(parents=True)
        (action_dir / "action.yaml").write_text(
            "runs:\n  using: composite\n  steps: []\n",
            encoding="UTF-8",
        )

    prep = ReleasePrep(workflow_dir=workflow_dir)
    assert prep.composite_action_names == ["alpha", "bravo", "charlie"]


def test_composite_action_names_empty_when_no_actions_dir(tmp_path: Path) -> None:
    """`composite_action_names` returns an empty list when actions/ is absent."""
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    prep = ReleasePrep(workflow_dir=workflow_dir)
    assert prep.composite_action_names == []


def test_freeze_unfreeze_round_trip_per_action(
    tmp_path: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Every discovered action is frozen and unfrozen idempotently.

    Parametrize-style conformance check: enumerate the population of
    discovered composite actions, run freeze then unfreeze, and assert each
    action's `@main` ↔ `@v{version}` substitution round-trips. Mirrors the
    "enumerate the population" pattern called out in `claude.md` § Testing.
    """
    monkeypatch.chdir(tmp_path)
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    actions_dir = tmp_path / ".github" / "actions"
    action_names = ("alpha", "bravo")
    for name in action_names:
        action_dir = actions_dir / name
        action_dir.mkdir(parents=True)
        (action_dir / "action.yaml").write_text(
            "runs:\n  using: composite\n  steps: []\n",
            encoding="UTF-8",
        )

    workflow_file = workflow_dir / "demo.yaml"
    references = "\n".join(
        f"      - uses: kdeldycke/repomatic/.github/actions/{name}@main"
        for name in action_names
    )
    workflow_file.write_text(
        "name: Demo\non: push\njobs:\n  demo:\n    steps:\n" + references + "\n",
        encoding="UTF-8",
    )

    prep = ReleasePrep(workflow_dir=workflow_dir)
    prep.freeze_workflow_urls()
    frozen = workflow_file.read_text(encoding="UTF-8")
    for name in action_names:
        assert f"actions/{name}@v1.2.3" in frozen, (
            f"Freeze did not rewrite the {name} action reference"
        )
        assert f"actions/{name}@main" not in frozen

    prep.unfreeze_workflow_urls()
    unfrozen = workflow_file.read_text(encoding="UTF-8")
    for name in action_names:
        assert f"actions/{name}@main" in unfrozen, (
            f"Unfreeze did not restore the {name} action reference"
        )
        assert f"actions/{name}@v1.2.3" not in unfrozen


def test_freeze_cli_version(
    tmp_path: Path,
    temp_workflows_with_cli: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that ``--from . repomatic`` is frozen to a PyPI version."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(workflow_dir=temp_workflows_with_cli)
    count = prep.freeze_cli_version("1.0.0")

    assert count == 2
    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "--from . repomatic" not in content
        assert "'repomatic==1.0.0'" in content


def test_freeze_workflow_urls(
    tmp_path: Path,
    temp_workflows: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that workflow URLs are frozen to versioned tag."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(workflow_dir=temp_workflows)
    count = prep.freeze_workflow_urls()

    assert count == 2
    for workflow_file in temp_workflows.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "/repomatic/main/" not in content
        assert "/repomatic/v1.2.3/" in content


def test_post_release(
    tmp_path: Path,
    temp_workflows: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test post-release workflow unfreezing."""
    monkeypatch.chdir(tmp_path)

    # First freeze for release.
    prep = ReleasePrep(workflow_dir=temp_workflows)
    prep.freeze_workflow_urls()

    # Then run post-release.
    modified = prep.post_release(update_workflows=True)

    assert len(modified) == 2
    for workflow_file in temp_workflows.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "/repomatic/main/" in content


def test_post_release_unfreezes_cli(
    tmp_path: Path,
    temp_workflows_with_cli: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that post-release unfreezes CLI invocations back to local source."""
    monkeypatch.chdir(tmp_path)

    # First freeze CLI.
    prep = ReleasePrep(workflow_dir=temp_workflows_with_cli)
    prep.freeze_cli_version("1.0.0")
    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "'repomatic==1.0.0'" in content

    # Then run post-release.
    prep.modified_files = []
    modified = prep.post_release(update_workflows=True)

    assert len(modified) == 2
    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "'repomatic==" not in content
        assert "--from . repomatic" in content


def test_prepare_release_full(
    tmp_path: Path,
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test full release preparation with all options."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows,
    )
    modified = prep.prepare_release(update_workflows=True)

    # Changelog once, citation once, 2 workflows for URLs.
    # CLI freeze doesn't match (no --from . repomatic in temp_workflows).
    assert len(modified) == 4
    assert len(set(modified)) == 4

    # Verify changelog changes.
    changelog_content = temp_changelog.read_text(encoding="UTF-8")
    assert "(unreleased)" not in changelog_content
    assert "...main)" not in changelog_content
    assert "[!IMPORTANT]" not in changelog_content

    # Verify citation changes.
    citation_content = temp_citation.read_text(encoding="UTF-8")
    assert f"date-released: {prep.release_date}" in citation_content

    # Verify workflow changes.
    for workflow_file in temp_workflows.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "/repomatic/v1.2.3/" in content


def test_prepare_release_freezes_cli(
    tmp_path: Path,
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows_with_cli: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that prepare_release freezes CLI invocations to current version."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows_with_cli,
    )
    prep.prepare_release(update_workflows=True)

    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "--from . repomatic" not in content
        assert "'repomatic==1.2.3'" in content


def test_prepare_release_without_workflows(
    tmp_path: Path,
    temp_changelog: Path,
    temp_citation: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test release preparation without workflow updates."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
    )
    modified = prep.prepare_release(update_workflows=False)

    # Changelog once, citation once.
    assert len(modified) == 2
    assert len(set(modified)) == 2


def test_release_date_format(tmp_path: Path, temp_pyproject: Path, monkeypatch) -> None:
    """Test that release date is in correct format."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    assert prep.release_date == today


def test_set_citation_release_date(
    tmp_path: Path, temp_citation: Path, temp_pyproject: Path, monkeypatch
) -> None:
    """Test that release date is set in citation file."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(citation_path=temp_citation)
    result = prep.set_citation_release_date()

    assert result is True
    content = temp_citation.read_text(encoding="UTF-8")
    assert "date-released: 2024-01-01" not in content
    assert f"date-released: {prep.release_date}" in content


def test_set_citation_release_date_missing_file(
    tmp_path: Path, temp_pyproject: Path, monkeypatch
) -> None:
    """Test that missing citation file is handled gracefully."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(citation_path=tmp_path / "nonexistent.cff")
    result = prep.set_citation_release_date()

    assert result is False


def test_unfreeze_action_reference(
    tmp_path: Path,
    temp_workflows_with_actions: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that composite action references are unfrozen to default branch."""
    monkeypatch.chdir(tmp_path)

    # First freeze the version.
    prep = ReleasePrep(workflow_dir=temp_workflows_with_actions)
    prep.freeze_workflow_urls()

    # Verify version is frozen.
    content = (temp_workflows_with_actions / "autofix.yaml").read_text(encoding="UTF-8")
    assert "@v1.2.3" in content

    # Then unfreeze to main.
    del prep.__dict__["current_version"]
    count = prep.unfreeze_workflow_urls()

    assert count == 1
    content = (temp_workflows_with_actions / "autofix.yaml").read_text(encoding="UTF-8")
    assert "@v1.2.3" not in content
    assert "@main" in content
    assert "kdeldycke/repomatic/.github/actions/pr-metadata@main" in content


def test_unfreeze_cli_version(
    tmp_path: Path,
    temp_workflows_with_cli: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that frozen PyPI version is unfrozen back to local source."""
    monkeypatch.chdir(tmp_path)

    # First freeze CLI.
    prep = ReleasePrep(workflow_dir=temp_workflows_with_cli)
    prep.freeze_cli_version("1.0.0")
    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "'repomatic==1.0.0'" in content

    # Then unfreeze.
    prep.modified_files = []
    count = prep.unfreeze_cli_version()

    assert count == 2
    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "'repomatic==" not in content
        assert "--from . repomatic" in content


def test_unfreeze_workflow_urls(
    tmp_path: Path,
    temp_workflows: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that workflow URLs are unfrozen back to default branch."""
    monkeypatch.chdir(tmp_path)

    # First freeze the version.
    prep = ReleasePrep(workflow_dir=temp_workflows)
    prep.freeze_workflow_urls()

    # Then unfreeze to main.
    # Need to clear cached property to re-read version.
    del prep.__dict__["current_version"]
    count = prep.unfreeze_workflow_urls()

    assert count == 2
    for workflow_file in temp_workflows.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "/repomatic/v1.2.3/" not in content
        assert "/repomatic/main/" in content


# --- JSON5 (renovate.json5) freeze/unfreeze tests ---


@pytest.fixture
def temp_renovate_configs(tmp_path: Path) -> list[Path]:
    """Create temporary renovate.json5 files at repo root and bundled data path."""
    json5_content = dedent("""\
        {
          packageRules: [
            {
              postUpgradeTasks: {
                commands: [
                  "bash -c 'install-tool uv latest && /opt/containerbase/tools/uv/*/*/bin/uvx --no-progress --from . repomatic update-checksums {{{packageFile}}}'",
                ],
              },
            },
            {
              postUpgradeTasks: {
                commands: [
                  "bash -c 'install-tool uv latest && /opt/containerbase/tools/uv/*/*/bin/uvx --no-progress --from . repomatic sync-uv-lock'",
                ],
              },
            },
          ],
        }
        """)

    root_json5 = tmp_path / "renovate.json5"
    root_json5.write_text(json5_content, encoding="UTF-8")

    bundled_dir = tmp_path / "repomatic" / "data"
    bundled_dir.mkdir(parents=True)
    bundled_json5 = bundled_dir / "renovate.json5"
    bundled_json5.write_text(json5_content, encoding="UTF-8")

    return [root_json5, bundled_json5]


def test_freeze_cli_version_renovate_json5(
    tmp_path: Path,
    temp_workflows_with_cli: Path,
    temp_renovate_configs: list[Path],
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that ``--from . repomatic`` is frozen to unquoted form in JSON5 files."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(workflow_dir=temp_workflows_with_cli)
    count = prep.freeze_cli_version("1.0.0")

    # 2 YAML files + 2 JSON5 files.
    assert count == 4
    for json5_file in temp_renovate_configs:
        content = json5_file.read_text(encoding="UTF-8")
        assert "--from . repomatic" not in content
        assert "repomatic==1.0.0" in content
        # Must not use quoted form in JSON5 (would break bash -c '...' quoting).
        assert "'repomatic==" not in content


def test_unfreeze_cli_version_renovate_json5(
    tmp_path: Path,
    temp_workflows_with_cli: Path,
    temp_renovate_configs: list[Path],
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that frozen PyPI version is unfrozen in JSON5 files."""
    monkeypatch.chdir(tmp_path)

    # First freeze.
    prep = ReleasePrep(workflow_dir=temp_workflows_with_cli)
    prep.freeze_cli_version("1.0.0")
    for json5_file in temp_renovate_configs:
        content = json5_file.read_text(encoding="UTF-8")
        assert "repomatic==1.0.0" in content

    # Then unfreeze.
    prep.modified_files = []
    count = prep.unfreeze_cli_version()

    # 2 YAML files + 2 JSON5 files.
    assert count == 4
    for json5_file in temp_renovate_configs:
        content = json5_file.read_text(encoding="UTF-8")
        assert "repomatic==" not in content
        assert "--from . repomatic" in content


# --- Readme binary download URL freeze tests ---


@pytest.fixture
def temp_readme(tmp_path: Path) -> Path:
    """Create a temporary readme with binary download table."""
    readme = tmp_path / "readme.md"
    readme.write_text(
        dedent("""\
            # My Project

            ### Executables

            | Platform    | `arm64` | `x86_64` |
            | :---------- | ------- | -------- |
            | **Linux**   | [Download `repomatic-linux-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-linux-arm64.bin) | [Download `repomatic-linux-x64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-linux-x64.bin) |
            | **macOS**   | [Download `repomatic-macos-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-macos-arm64.bin) | [Download `repomatic-macos-x64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-macos-x64.bin) |
            | **Windows** | [Download `repomatic-windows-arm64.exe`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-windows-arm64.exe) | [Download `repomatic-windows-x64.exe`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-windows-x64.exe) |
            """),
        encoding="UTF-8",
    )
    return readme


@pytest.fixture
def temp_readme_frozen(tmp_path: Path) -> Path:
    """Create a temporary readme with previously frozen download URLs."""
    readme = tmp_path / "readme.md"
    readme.write_text(
        dedent("""\
            # My Project

            ### Executables

            | Platform    | `arm64` | `x86_64` |
            | :---------- | ------- | -------- |
            | **Linux**   | [Download `repomatic-1.2.2-linux-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v1.2.2/repomatic-1.2.2-linux-arm64.bin) | [Download `repomatic-1.2.2-linux-x64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v1.2.2/repomatic-1.2.2-linux-x64.bin) |
            | **macOS**   | [Download `repomatic-1.2.2-macos-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v1.2.2/repomatic-1.2.2-macos-arm64.bin) | [Download `repomatic-1.2.2-macos-x64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v1.2.2/repomatic-1.2.2-macos-x64.bin) |
            | **Windows** | [Download `repomatic-1.2.2-windows-arm64.exe`](https://github.com/kdeldycke/repomatic/releases/download/v1.2.2/repomatic-1.2.2-windows-arm64.exe) | [Download `repomatic-1.2.2-windows-x64.exe`](https://github.com/kdeldycke/repomatic/releases/download/v1.2.2/repomatic-1.2.2-windows-x64.exe) |
            """),
        encoding="UTF-8",
    )
    return readme


def test_freeze_readme_download_urls(
    tmp_path: Path,
    temp_readme: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that initial ``/releases/latest/download/`` URLs are frozen."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(readme_path=temp_readme)
    result = prep.freeze_readme_download_urls("1.2.3")

    assert result is True
    content = temp_readme.read_text(encoding="UTF-8")
    assert "/releases/latest/download/" not in content
    assert "/releases/download/v1.2.3/" in content
    assert "repomatic-1.2.3-linux-arm64.bin" in content
    assert "repomatic-1.2.3-linux-x64.bin" in content
    assert "repomatic-1.2.3-macos-arm64.bin" in content
    assert "repomatic-1.2.3-macos-x64.bin" in content
    assert "repomatic-1.2.3-windows-arm64.exe" in content
    assert "repomatic-1.2.3-windows-x64.exe" in content


def test_freeze_readme_download_urls_already_frozen(
    tmp_path: Path,
    temp_readme_frozen: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that previously frozen URLs are re-frozen to new version."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(readme_path=temp_readme_frozen)
    result = prep.freeze_readme_download_urls("1.2.3")

    assert result is True
    content = temp_readme_frozen.read_text(encoding="UTF-8")
    assert "v1.2.2" not in content
    assert "1.2.2" not in content
    assert "/releases/download/v1.2.3/" in content
    assert "repomatic-1.2.3-linux-arm64.bin" in content
    assert "repomatic-1.2.3-windows-x64.exe" in content


def test_freeze_readme_download_urls_updates_link_text(
    tmp_path: Path,
    temp_readme: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that display text in markdown links is also updated."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(readme_path=temp_readme)
    prep.freeze_readme_download_urls("1.2.3")

    content = temp_readme.read_text(encoding="UTF-8")
    # Display text inside backticks should be updated.
    assert "`repomatic-1.2.3-linux-arm64.bin`" in content
    assert "`repomatic-1.2.3-windows-x64.exe`" in content
    # Old versionless names should not remain.
    assert "`repomatic-linux-arm64.bin`" not in content
    assert "`repomatic-windows-x64.exe`" not in content


def test_freeze_readme_missing_file(
    tmp_path: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that missing readme is handled gracefully."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(readme_path=tmp_path / "nonexistent.md")
    result = prep.freeze_readme_download_urls("1.2.3")

    assert result is False


def test_prepare_release_freezes_readme(
    tmp_path: Path,
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows: Path,
    temp_readme: Path,
    temp_pyproject: Path,
    monkeypatch,
) -> None:
    """Test that ``prepare_release(update_workflows=True)`` freezes readme URLs."""
    monkeypatch.chdir(tmp_path)

    prep = ReleasePrep(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows,
        readme_path=temp_readme,
    )
    modified = prep.prepare_release(update_workflows=True)

    assert temp_readme in modified
    content = temp_readme.read_text(encoding="UTF-8")
    assert "/releases/latest/download/" not in content
    assert "/releases/download/v1.2.3/" in content
    assert "repomatic-1.2.3-linux-arm64.bin" in content
