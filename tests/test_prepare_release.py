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

"""Tests for `repomatic.release.prepare_release`: the release freeze and unfreeze edits."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from repomatic.release.prepare_release import (
    LOCAL_CLI_INVOCATION,
    SELF_PIN_COOLDOWN_EXEMPTION,
    PrepareRelease,
)
from repomatic.release.version_sync import (
    apply_self_pin_exemption,
    frozen_cli_invocation,
)
from repomatic.tooling.plugin import PLUGIN_ROOT
from tests.conftest import PROJECT_ROOT

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator


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
    """Create workflows with lockfile-backed `uv run` CLI invocations."""
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
                      uv --no-progress run --frozen -- repomatic
                      metadata --output "$GITHUB_OUTPUT"
              lint:
                steps:
                  - run: >
                      uv --no-progress run --frozen -- repomatic
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
                      uv --no-progress run --frozen -- repomatic
                      metadata --output "$GITHUB_OUTPUT"
                  - run: uv --no-progress run --frozen -- repomatic pr-body
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


@pytest.fixture(autouse=True)
def _in_project(tmp_path: Path, temp_pyproject: Path, monkeypatch) -> None:
    """Run every test from inside a throwaway project holding a `pyproject.toml`.

    `PrepareRelease` reads its version from the `pyproject.toml` of the current
    working directory, so the file and the `chdir` are one precondition, not two.

    ```{caution}
    This is `autouse` for containment, not convenience. Every `PrepareRelease`
    path left unset resolves against the *current directory*
    (`Path("./docs/install.md").resolve()` and friends), so a single test in
    this module running from the repository root points the freeze and unfreeze
    writers at the real `docs/install.md` and `.claude-plugin/marketplace.json`
    and rewrites them in place. Passing `workflow_dir=` is not protection: it
    redirects one path and leaves the other three aimed at the checkout. Opting
    a test out of this fixture re-arms that, which
    `_repo_files_survive_the_module` is here to catch.
    ```
    """
    monkeypatch.chdir(tmp_path)


# Files a stray `PrepareRelease` writes to when a test escapes `_in_project`.
LEAK_CANARIES = ("docs/install.md", ".claude-plugin/marketplace.json")


@pytest.fixture(scope="module", autouse=True)
def _repo_files_survive_the_module() -> Iterator[None]:
    """Fail loudly if this module rewrites the checkout it runs in.

    A leak here is silent by construction: the tests still pass, and the damage
    surfaces later as an unexplained dirty tree or as two unrelated
    `test_plugin` failures. Snapshotting the files a leak would touch turns that
    into a failure in the module that caused it.
    """
    before = {
        name: (PROJECT_ROOT / name).read_bytes()
        for name in LEAK_CANARIES
        if (PROJECT_ROOT / name).exists()
    }

    yield

    rewritten = sorted(
        name
        for name, content in before.items()
        if (PROJECT_ROOT / name).read_bytes() != content
    )
    assert not rewritten, (
        f"tests/test_prepare_release.py rewrote the real repository: {rewritten}."
        " A test built PrepareRelease from a directory other than its tmp_path,"
        " so the unset paths resolved against the checkout. Restore the files"
        " with `git checkout --` and keep every test under the autouse"
        " `_in_project` fixture."
    )


def test_current_version_from_pyproject() -> None:
    """Test that current version is read from pyproject.toml."""
    prep = PrepareRelease()

    assert prep.current_version == "1.2.3"


def test_freeze_action_reference(temp_workflows_with_actions: Path) -> None:
    """Test that composite action references are frozen to versioned tag."""
    prep = PrepareRelease(workflow_dir=temp_workflows_with_actions)
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

    prep = PrepareRelease(workflow_dir=workflow_dir)
    assert prep.composite_action_names == ["alpha", "bravo", "charlie"]


def test_composite_action_names_empty_when_no_actions_dir(tmp_path: Path) -> None:
    """`composite_action_names` returns an empty list when actions/ is absent."""
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    prep = PrepareRelease(workflow_dir=workflow_dir)
    assert prep.composite_action_names == []


def test_freeze_unfreeze_round_trip_per_action(tmp_path: Path) -> None:
    """Every discovered action is frozen and unfrozen idempotently.

    Parametrize-style conformance check: enumerate the population of
    discovered composite actions, run freeze then unfreeze, and assert each
    action's `@main` ↔ `@v{version}` substitution round-trips. Mirrors the
    "enumerate the population" pattern called out in `claude.md` § Testing.
    """
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

    prep = PrepareRelease(workflow_dir=workflow_dir)
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


def test_freeze_cli_version(temp_workflows_with_cli: Path) -> None:
    """Test that the local `uv run` invocation is frozen to a PyPI version."""
    prep = PrepareRelease(workflow_dir=temp_workflows_with_cli)
    count = prep.freeze_cli_version("1.0.0")

    assert count == 2
    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert LOCAL_CLI_INVOCATION not in content
        assert "'repomatic==1.0.0'" in content


def test_freeze_workflow_urls(temp_workflows: Path) -> None:
    """Test that workflow URLs are frozen to versioned tag."""
    prep = PrepareRelease(workflow_dir=temp_workflows)
    count = prep.freeze_workflow_urls()

    assert count == 2
    for workflow_file in temp_workflows.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "/repomatic/main/" not in content
        assert "/repomatic/v1.2.3/" in content


def test_post_release(temp_workflows: Path) -> None:
    """Test post-release workflow unfreezing."""
    # First freeze for release.
    prep = PrepareRelease(workflow_dir=temp_workflows)
    prep.freeze_workflow_urls()

    # Then run post-release.
    modified = prep.post_release(update_workflows=True)

    assert len(modified) == 2
    for workflow_file in temp_workflows.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "/repomatic/main/" in content


def test_post_release_unfreezes_cli(temp_workflows_with_cli: Path) -> None:
    """Test that post-release unfreezes CLI invocations back to local source."""
    # First freeze CLI.
    prep = PrepareRelease(workflow_dir=temp_workflows_with_cli)
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
        assert LOCAL_CLI_INVOCATION in content


def test_prepare_release_full(
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows: Path,
) -> None:
    """Test full release preparation with all options."""
    prep = PrepareRelease(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows,
    )
    modified = prep.prepare_release(update_workflows=True)

    # Changelog once, citation once, 2 workflows for URLs.
    # CLI freeze doesn't match (no local invocation in temp_workflows).
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
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows_with_cli: Path,
) -> None:
    """Test that prepare_release freezes CLI invocations to current version."""
    prep = PrepareRelease(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows_with_cli,
    )
    prep.prepare_release(update_workflows=True)

    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert LOCAL_CLI_INVOCATION not in content
        assert "'repomatic==1.2.3'" in content


def test_prepare_release_without_workflows(
    temp_changelog: Path,
    temp_citation: Path,
) -> None:
    """Test release preparation without workflow updates."""
    prep = PrepareRelease(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
    )
    modified = prep.prepare_release(update_workflows=False)

    # Changelog once, citation once.
    assert len(modified) == 2
    assert len(set(modified)) == 2


def test_release_date_format() -> None:
    """Test that release date is in correct format."""
    prep = PrepareRelease()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    assert prep.release_date == today


def test_set_citation_release_date(temp_citation: Path) -> None:
    """Test that release date is set in citation file."""
    prep = PrepareRelease(citation_path=temp_citation)
    result = prep.set_citation_release_date()

    assert result is True
    content = temp_citation.read_text(encoding="UTF-8")
    assert "date-released: 2024-01-01" not in content
    assert f"date-released: {prep.release_date}" in content


def test_set_citation_release_date_missing_file(tmp_path: Path) -> None:
    """Test that missing citation file is handled gracefully."""
    prep = PrepareRelease(citation_path=tmp_path / "nonexistent.cff")
    result = prep.set_citation_release_date()

    assert result is False


def test_unfreeze_action_reference(temp_workflows_with_actions: Path) -> None:
    """Test that composite action references are unfrozen to default branch."""
    # First freeze the version.
    prep = PrepareRelease(workflow_dir=temp_workflows_with_actions)
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


def test_unfreeze_cli_version(temp_workflows_with_cli: Path) -> None:
    """Test that frozen PyPI version is unfrozen back to local source."""
    # First freeze CLI.
    prep = PrepareRelease(workflow_dir=temp_workflows_with_cli)
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
        assert LOCAL_CLI_INVOCATION in content


def test_spliced_exemption_round_trips_through_unfreeze(
    temp_workflows_with_cli: Path,
) -> None:
    """A pin exempted by `sync-workflow-pins` unfreezes like a frozen one.

    The freeze and `apply_self_pin_exemption` are two writers of the same
    command line: they must converge on one byte sequence, or the unfreeze
    pattern silently skips whichever spelling it does not recognize.
    """
    prep = PrepareRelease(workflow_dir=temp_workflows_with_cli)
    frozen = frozen_cli_invocation("repomatic", "1.0.0", SELF_PIN_COOLDOWN_EXEMPTION)

    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        # An exemption-less pin, as an older freeze wrote it, spliced by the
        # sync path: the result must equal the current freeze spelling.
        bare = workflow_file.read_text(encoding="UTF-8").replace(
            LOCAL_CLI_INVOCATION, "uvx --no-progress 'repomatic==1.0.0'"
        )
        spliced = apply_self_pin_exemption(
            bare, "repomatic", SELF_PIN_COOLDOWN_EXEMPTION
        )
        assert frozen in spliced
        workflow_file.write_text(spliced, encoding="UTF-8")

    count = prep.unfreeze_cli_version()

    assert count == 2
    for workflow_file in temp_workflows_with_cli.glob("*.yaml"):
        content = workflow_file.read_text(encoding="UTF-8")
        assert "'repomatic==" not in content
        assert SELF_PIN_COOLDOWN_EXEMPTION not in content
        assert LOCAL_CLI_INVOCATION in content


def test_unfreeze_workflow_urls(temp_workflows: Path) -> None:
    """Test that workflow URLs are unfrozen back to default branch."""
    # First freeze the version.
    prep = PrepareRelease(workflow_dir=temp_workflows)
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


# --- Readme binary download URL freeze tests ---


@pytest.fixture
def temp_install(tmp_path: Path) -> Path:
    """Create a temporary install guide with binary download table."""
    install_md = tmp_path / "install.md"
    install_md.write_text(
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
    return install_md


@pytest.fixture
def temp_install_frozen(tmp_path: Path) -> Path:
    """Create a temporary install guide with previously frozen download URLs."""
    install_md = tmp_path / "install.md"
    install_md.write_text(
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
    return install_md


def test_freeze_install_download_urls(temp_install: Path) -> None:
    """Test that initial ``/releases/latest/download/`` URLs are frozen."""
    prep = PrepareRelease(install_path=temp_install)
    result = prep.freeze_install_download_urls("1.2.3")

    assert result is True
    content = temp_install.read_text(encoding="UTF-8")
    assert "/releases/latest/download/" not in content
    assert "/releases/download/v1.2.3/" in content
    assert "repomatic-1.2.3-linux-arm64.bin" in content
    assert "repomatic-1.2.3-linux-x64.bin" in content
    assert "repomatic-1.2.3-macos-arm64.bin" in content
    assert "repomatic-1.2.3-macos-x64.bin" in content
    assert "repomatic-1.2.3-windows-arm64.exe" in content
    assert "repomatic-1.2.3-windows-x64.exe" in content


def test_freeze_install_download_urls_already_frozen(temp_install_frozen: Path) -> None:
    """Test that previously frozen URLs are re-frozen to new version."""
    prep = PrepareRelease(install_path=temp_install_frozen)
    result = prep.freeze_install_download_urls("1.2.3")

    assert result is True
    content = temp_install_frozen.read_text(encoding="UTF-8")
    assert "v1.2.2" not in content
    assert "1.2.2" not in content
    assert "/releases/download/v1.2.3/" in content
    assert "repomatic-1.2.3-linux-arm64.bin" in content
    assert "repomatic-1.2.3-windows-x64.exe" in content


def test_freeze_install_download_urls_updates_link_text(temp_install: Path) -> None:
    """Test that display text in markdown links is also updated."""
    prep = PrepareRelease(install_path=temp_install)
    prep.freeze_install_download_urls("1.2.3")

    content = temp_install.read_text(encoding="UTF-8")
    # Display text inside backticks should be updated.
    assert "`repomatic-1.2.3-linux-arm64.bin`" in content
    assert "`repomatic-1.2.3-windows-x64.exe`" in content
    # Old versionless names should not remain.
    assert "`repomatic-linux-arm64.bin`" not in content
    assert "`repomatic-windows-x64.exe`" not in content


def test_freeze_install_missing_file(tmp_path: Path) -> None:
    """Test that a missing install guide is handled gracefully."""
    prep = PrepareRelease(install_path=tmp_path / "nonexistent.md")
    result = prep.freeze_install_download_urls("1.2.3")

    assert result is False


def _marketplace(
    tmp_path: Path,
    ref: str | None = "v1.2.2",
    version: str | None = "1.2.2",
) -> Path:
    """Write a minimal plugin marketplace pinned to *ref* and *version*.

    Either half is omitted when passed `None`, which is the never-released state:
    an entry tracking the default branch, with no pin for the freeze to move.
    """
    source = {
        "path": PLUGIN_ROOT,
        "source": "git-subdir",
        "url": "https://github.com/kdeldycke/repomatic",
    }
    if ref is not None:
        source["ref"] = ref
    entry = {"name": "repomatic", "source": source}
    if version is not None:
        entry["version"] = version

    target = tmp_path / "marketplace.json"
    target.write_text(
        json.dumps({
            "name": "kdeldycke",
            "owner": {"name": "Kevin Deldycke"},
            "plugins": [entry],
        }),
        encoding="UTF-8",
    )
    return target


def test_freeze_marketplace_pin(tmp_path: Path) -> None:
    """Both halves of an already-pinned entry ratchet to the new release."""
    target = _marketplace(tmp_path)

    prep = PrepareRelease(marketplace_path=target)
    assert prep.freeze_marketplace_pin("1.2.3") is True

    content = target.read_text(encoding="UTF-8")
    assert "1.2.2" not in content
    # Still valid JSON, and only the pin moved.
    entry = json.loads(content)["plugins"][0]
    assert entry["name"] == "repomatic"
    assert entry["version"] == "1.2.3"
    assert entry["source"]["source"] == "git-subdir"
    assert entry["source"]["ref"] == "v1.2.3"
    assert entry["source"]["path"] == PLUGIN_ROOT


def test_freeze_marketplace_pin_leaves_an_unpinned_entry_alone(tmp_path: Path) -> None:
    """A never-released catalog tracks the default branch until a tag exists."""
    target = _marketplace(tmp_path, ref=None, version=None)
    before = target.read_text(encoding="UTF-8")

    prep = PrepareRelease(marketplace_path=target)
    assert prep.freeze_marketplace_pin("1.2.3") is False
    assert target.read_text(encoding="UTF-8") == before


def test_freeze_marketplace_pin_missing_file(tmp_path: Path) -> None:
    """A repository with no plugin marketplace is left alone."""
    prep = PrepareRelease(marketplace_path=tmp_path / "nonexistent.json")
    assert prep.freeze_marketplace_pin("1.2.3") is False


def test_freeze_plugin_manifest_version(tmp_path: Path) -> None:
    """The manifest a `git-subdir` source publishes names the new release."""
    target = tmp_path / "plugin.json"
    target.write_text(
        json.dumps({"name": "repomatic", "version": "1.2.2"}), encoding="UTF-8"
    )

    prep = PrepareRelease(plugin_manifest_path=target)
    assert prep.freeze_plugin_manifest_version("1.2.3") is True
    assert json.loads(target.read_text(encoding="UTF-8"))["version"] == "1.2.3"


def test_freeze_plugin_manifest_version_missing_file(tmp_path: Path) -> None:
    """A repository shipping no plugin is left alone."""
    prep = PrepareRelease(plugin_manifest_path=tmp_path / "nonexistent.json")
    assert prep.freeze_plugin_manifest_version("1.2.3") is False


def test_post_release_leaves_the_plugin_manifest_version(
    tmp_path: Path,
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows: Path,
) -> None:
    """The unfreeze must not walk the manifest back to a `.devN` version.

    This is why the version is written here rather than by bump-my-version, which
    runs on both release commits: the post-release bump would leave the published
    manifest advertising a `X.Y.Z.devN` release that was never tagged.
    """
    target = tmp_path / "plugin.json"
    target.write_text(
        json.dumps({"name": "repomatic", "version": "1.2.3"}), encoding="UTF-8"
    )

    prep = PrepareRelease(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows,
        plugin_manifest_path=target,
    )
    prep.post_release(update_workflows=True)

    assert json.loads(target.read_text(encoding="UTF-8"))["version"] == "1.2.3"


def test_post_release_leaves_the_marketplace_pin(
    tmp_path: Path,
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows: Path,
) -> None:
    """The unfreeze must not walk the pin back to a `.devN` tag.

    The ratchet is the whole reason this pin is not a bump-my-version entry: the
    post-release bump would rewrite it to a `vX.Y.Z.devN` tag that never exists,
    breaking `/plugin install` for the entire development cycle.
    """
    target = _marketplace(tmp_path, ref="v1.2.3", version="1.2.3")

    prep = PrepareRelease(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows,
        marketplace_path=target,
    )
    prep.post_release(update_workflows=True)

    entry = json.loads(target.read_text(encoding="UTF-8"))["plugins"][0]
    assert entry["source"]["ref"] == "v1.2.3"
    assert entry["version"] == "1.2.3"


def test_prepare_release_freezes_install(
    temp_changelog: Path,
    temp_citation: Path,
    temp_workflows: Path,
    temp_install: Path,
) -> None:
    """Test that ``prepare_release(update_workflows=True)`` freezes install URLs."""
    prep = PrepareRelease(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        workflow_dir=temp_workflows,
        install_path=temp_install,
    )
    modified = prep.prepare_release(update_workflows=True)

    assert temp_install in modified
    content = temp_install.read_text(encoding="UTF-8")
    assert "/releases/latest/download/" not in content
    assert "/releases/download/v1.2.3/" in content
    assert "repomatic-1.2.3-linux-arm64.bin" in content


# --- Install guide CLI version pin freeze tests ---


@pytest.fixture
def temp_install_pinned(tmp_path: Path) -> Path:
    """Create a temporary install guide with pinned CLI version examples."""
    install_md = tmp_path / "install.md"
    install_md.write_text(
        dedent("""\
            # My Project

            ## Try it

            ```shell-session
            $ uvx test-project@1.2.2
            ```

            ```shell-session
            $ uvx -- test-project==1.2.2 --help
            ```

            A distro package is available as `python-test-project==0.9` too.
            Development runs use `uvx --from git+https://example.com/repo -- test-project`.
            """),
        encoding="UTF-8",
    )
    return install_md


def test_freeze_install_cli_version(temp_install_pinned: Path) -> None:
    """Test that pinned `@` and `==` CLI examples are bumped to the release."""
    prep = PrepareRelease(install_path=temp_install_pinned)
    result = prep.freeze_install_cli_version("1.2.3")

    assert result is True
    content = temp_install_pinned.read_text(encoding="UTF-8")
    assert "test-project@1.2.3" in content
    assert "test-project==1.2.3" in content
    assert "1.2.2" not in content


def test_freeze_install_cli_version_leaves_other_pins(
    temp_install_pinned: Path,
) -> None:
    """Test that prefixed package names and git refs are left untouched."""
    prep = PrepareRelease(install_path=temp_install_pinned)
    prep.freeze_install_cli_version("1.2.3")

    content = temp_install_pinned.read_text(encoding="UTF-8")
    assert "python-test-project==0.9" in content
    assert "git+https://example.com/repo -- test-project" in content


def test_freeze_install_cli_version_idempotent(temp_install_pinned: Path) -> None:
    """Test that re-freezing to the same version is a no-op."""
    prep = PrepareRelease(install_path=temp_install_pinned)
    assert prep.freeze_install_cli_version("1.2.3") is True
    assert prep.freeze_install_cli_version("1.2.3") is False


def test_freeze_install_cli_version_missing_file(tmp_path: Path) -> None:
    """Test that a missing install guide is handled gracefully."""
    prep = PrepareRelease(install_path=tmp_path / "nonexistent.md")
    result = prep.freeze_install_cli_version("1.2.3")

    assert result is False


def test_prepare_release_pins_install_cli(
    temp_changelog: Path,
    temp_citation: Path,
    temp_install_pinned: Path,
) -> None:
    """Test that ``prepare_release()`` pins CLI examples without workflows."""
    prep = PrepareRelease(
        changelog_path=temp_changelog,
        citation_path=temp_citation,
        install_path=temp_install_pinned,
    )
    modified = prep.prepare_release()

    assert temp_install_pinned in modified
    content = temp_install_pinned.read_text(encoding="UTF-8")
    assert "test-project@1.2.3" in content
    assert "test-project==1.2.3" in content


# ---------------------------------------------------------------------------
# Cooldown exemption on the frozen self-pin
# ---------------------------------------------------------------------------

REPO_WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"

YAMLLINT_LINE_LENGTH = 120
"""Column cap from the bundled `yamllint.yaml`, mirrored here.

Not imported from the YAML: this test asserts the *frozen* workflows satisfy the
same rule the `lint-yaml` job applies to the working tree, and reading the config
would make the test pass by construction if the cap were ever loosened to
accommodate a freeze that outgrew it.
"""


def _breaches_line_length(line: str) -> bool:
    """Whether *line* violates yamllint's `line-length` as this repo configures it.

    Ports the `allow-non-breakable-words` branch of yamllint's rule rather than
    comparing raw lengths: the bundled config enables it, so an over-long line
    made of a single unbreakable token (a release download URL) is legal and must
    not be reported here. Leading indentation and a `#` or `- ` marker are
    skipped first, matching yamllint; what is left counts only if it contains a
    space, meaning it *could* have been wrapped.
    """
    if len(line) <= YAMLLINT_LINE_LENGTH:
        return False
    rest = line.lstrip(" ")
    if rest.startswith("#"):
        rest = rest.lstrip("#")[1:]
    elif rest.startswith("- "):
        rest = rest[2:]
    return " " in rest


def _freeze_repo_workflows(tmp_path: Path, version: str = "1.2.3") -> Path:
    """Copy the real workflows and run the release freeze over them."""
    frozen = tmp_path / "workflows"
    shutil.copytree(REPO_WORKFLOWS, frozen)
    PrepareRelease(workflow_dir=frozen).freeze_cli_version(version)
    return frozen


def test_freeze_keeps_workflows_within_line_length(tmp_path: Path) -> None:
    """Freezing the real workflows must not breach yamllint's column cap.

    The freeze swaps the local `uv run` invocation for the pin *plus* its cooldown
    exemption, which is markedly longer, and it is a blind string replacement: a
    line that fits on `main` can overflow only once frozen. Nothing else catches
    that, because the frozen state is never committed to `main` and only exists
    on the release commit, where the failure would surface as a red `lint-yaml`
    on every release.
    """
    offenders = [
        f"{path.name}:{num} ({len(line)} chars) {line.strip()[:60]}"
        for path in sorted(_freeze_repo_workflows(tmp_path).glob("*.yaml"))
        for num, line in enumerate(path.read_text(encoding="UTF-8").splitlines(), 1)
        if _breaches_line_length(line)
    ]
    assert not offenders, (
        "Freezing pushes these lines past yamllint's "
        f"{YAMLLINT_LINE_LENGTH}-column cap; wrap them in the source workflow "
        "so they still fit once the pin and its exemption are spliced in:\n"
        + "\n".join(offenders)
    )


def test_freeze_exempts_self_pin_from_cooldown(tmp_path: Path) -> None:
    """Every frozen invocation carries the cooldown escape hatch.

    Without it the workflow-wide `UV_EXCLUDE_NEWER` would refuse the pin the
    freeze just wrote, since it names a release published minutes earlier.
    """
    for path in sorted(_freeze_repo_workflows(tmp_path).glob("*.yaml")):
        for num, line in enumerate(path.read_text(encoding="UTF-8").splitlines(), 1):
            if "'repomatic==1.2.3'" in line and not line.lstrip().startswith("#"):
                assert SELF_PIN_COOLDOWN_EXEMPTION in line, (
                    f"{path.name}:{num} pins repomatic without the cooldown "
                    f"exemption: {line.strip()}"
                )


def test_freeze_unfreeze_round_trips(tmp_path: Path) -> None:
    """Unfreezing restores the source byte for byte, exemption included."""
    frozen = _freeze_repo_workflows(tmp_path)
    PrepareRelease(workflow_dir=frozen).unfreeze_cli_version()
    for path in sorted(frozen.glob("*.yaml")):
        original = (REPO_WORKFLOWS / path.name).read_text(encoding="UTF-8")
        assert path.read_text(encoding="UTF-8") == original, (
            f"{path.name} did not round-trip through freeze/unfreeze"
        )
