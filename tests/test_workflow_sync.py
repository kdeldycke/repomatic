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

"""Tests for workflow sync module."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from repomatic.cli import _apply_workflow_config
from repomatic.github.actions import AnnotationLevel
from repomatic.github.workflow_sync import (
    ALL_WORKFLOW_FILES,
    DEFAULT_REPO,
    NON_REUSABLE_WORKFLOWS,
    REUSABLE_WORKFLOWS,
    LintResult,
    WorkflowFormat,
    WorkflowTriggerInfo,
    check_has_workflow_dispatch,
    check_secrets_passed,
    check_triggers_match,
    check_version_pinned,
    extract_trigger_info,
    generate_thin_caller,
    generate_workflow_header,
    generate_workflows,
    identify_canonical_workflow,
    run_workflow_lint,
)


def test_reusable_workflows_sorted() -> None:
    """Verify reusable workflows are sorted."""
    assert list(REUSABLE_WORKFLOWS) == sorted(REUSABLE_WORKFLOWS)


def test_all_workflow_files_sorted() -> None:
    """Verify all workflow files are sorted."""
    assert list(ALL_WORKFLOW_FILES) == sorted(ALL_WORKFLOW_FILES)


def test_non_reusable_subset_of_all() -> None:
    """Verify non-reusable workflows are a subset of all workflows."""
    assert NON_REUSABLE_WORKFLOWS <= set(ALL_WORKFLOW_FILES)


def test_reusable_subset_of_all() -> None:
    """Verify reusable workflows are a subset of all workflows."""
    assert set(REUSABLE_WORKFLOWS) <= set(ALL_WORKFLOW_FILES)


def test_no_overlap() -> None:
    """Verify no overlap between reusable and non-reusable sets."""
    assert not (set(REUSABLE_WORKFLOWS) & NON_REUSABLE_WORKFLOWS)


def test_union_is_all() -> None:
    """Verify union of reusable and non-reusable equals all."""
    assert set(REUSABLE_WORKFLOWS) | NON_REUSABLE_WORKFLOWS == set(ALL_WORKFLOW_FILES)


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_reusable_has_workflow_call(filename: str) -> None:
    """Verify all reusable workflows have workflow_call trigger."""
    info = extract_trigger_info(filename)
    assert info.has_workflow_call is True


@pytest.mark.parametrize("filename", list(NON_REUSABLE_WORKFLOWS))
def test_non_reusable_no_workflow_call(filename: str) -> None:
    """Verify non-reusable workflows lack workflow_call trigger."""
    info = extract_trigger_info(filename)
    assert info.has_workflow_call is False


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_returns_trigger_info(filename: str) -> None:
    """Verify return type is WorkflowTriggerInfo."""
    info = extract_trigger_info(filename)
    assert isinstance(info, WorkflowTriggerInfo)
    assert info.filename == filename
    assert isinstance(info.name, str)
    assert len(info.name) > 0


def test_autofix_has_secrets() -> None:
    """Verify autofix.yaml defines secrets."""
    info = extract_trigger_info("autofix.yaml")
    assert "WORKFLOW_UPDATE_GITHUB_PAT" in info.call_secrets


def test_changelog_has_secrets() -> None:
    """Verify changelog.yaml defines secrets."""
    info = extract_trigger_info("changelog.yaml")
    assert "WORKFLOW_UPDATE_GITHUB_PAT" in info.call_secrets


def test_release_has_secrets() -> None:
    """Verify release.yaml defines secrets."""
    info = extract_trigger_info("release.yaml")
    assert "PYPI_TOKEN" in info.call_secrets
    assert "WORKFLOW_UPDATE_GITHUB_PAT" in info.call_secrets


def test_renovate_has_secrets() -> None:
    """Verify renovate.yaml defines secrets."""
    info = extract_trigger_info("renovate.yaml")
    assert "WORKFLOW_UPDATE_GITHUB_PAT" in info.call_secrets


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_no_workflow_call_inputs(filename: str) -> None:
    """Verify no reusable workflow defines ``workflow_call`` inputs.

    All configurable options live in ``[tool.repomatic]`` in ``pyproject.toml``.
    Workflows read config via ``repomatic`` CLI instead of accepting inputs.
    """
    info = extract_trigger_info(filename)
    assert len(info.call_inputs) == 0, (
        f"{filename} still defines workflow_call inputs: {sorted(info.call_inputs)}"
    )


def test_changelog_has_workflow_run() -> None:
    """Verify changelog.yaml has workflow_run trigger."""
    info = extract_trigger_info("changelog.yaml")
    assert "workflow_run" in info.non_call_triggers


def test_autolock_has_schedule() -> None:
    """Verify autolock.yaml has schedule trigger."""
    info = extract_trigger_info("autolock.yaml")
    assert "schedule" in info.non_call_triggers


def test_nonexistent_file() -> None:
    """Raise FileNotFoundError for missing workflow."""
    with pytest.raises(FileNotFoundError):
        extract_trigger_info("nonexistent.yaml")


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_generates_valid_yaml(filename: str) -> None:
    """Verify generated content is valid YAML."""
    content = generate_thin_caller(filename)
    data = yaml.safe_load(content)
    assert isinstance(data, dict)


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_includes_workflow_dispatch(filename: str) -> None:
    """Verify generated caller always includes workflow_dispatch."""
    content = generate_thin_caller(filename)
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    assert "workflow_dispatch" in triggers


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_correct_uses_ref(filename: str) -> None:
    """Verify correct uses reference in job."""
    content = generate_thin_caller(filename)
    expected = f"{DEFAULT_REPO}/.github/workflows/{filename}@main"
    assert expected in content


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_starts_with_document_marker(filename: str) -> None:
    """Verify generated YAML starts with --- document marker."""
    content = generate_thin_caller(filename)
    assert content.startswith("---\n")


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_quoted_on_key(filename: str) -> None:
    """Verify the on key is quoted as ``"on":``."""
    content = generate_thin_caller(filename)
    assert '"on":' in content


def test_autofix_passes_secrets_explicitly() -> None:
    """Verify autofix.yaml thin caller passes secrets explicitly."""
    content = generate_thin_caller("autofix.yaml")
    assert "secrets: inherit" not in content
    assert (
        "WORKFLOW_UPDATE_GITHUB_PAT: ${{ secrets.WORKFLOW_UPDATE_GITHUB_PAT }}"
        in content
    )


def test_changelog_passes_secrets_explicitly() -> None:
    """Verify changelog.yaml thin caller passes secrets explicitly."""
    content = generate_thin_caller("changelog.yaml")
    assert "secrets: inherit" not in content
    assert (
        "WORKFLOW_UPDATE_GITHUB_PAT: ${{ secrets.WORKFLOW_UPDATE_GITHUB_PAT }}"
        in content
    )


def test_release_passes_secrets_explicitly() -> None:
    """Verify release.yaml thin caller passes secrets explicitly."""
    content = generate_thin_caller("release.yaml")
    assert "secrets: inherit" not in content
    assert "PYPI_TOKEN: ${{ secrets.PYPI_TOKEN }}" in content
    assert (
        "WORKFLOW_UPDATE_GITHUB_PAT: ${{ secrets.WORKFLOW_UPDATE_GITHUB_PAT }}"
        in content
    )


def test_renovate_passes_secrets_explicitly() -> None:
    """Verify renovate.yaml thin caller passes secrets explicitly."""
    content = generate_thin_caller("renovate.yaml")
    assert "secrets: inherit" not in content
    assert (
        "WORKFLOW_UPDATE_GITHUB_PAT: ${{ secrets.WORKFLOW_UPDATE_GITHUB_PAT }}"
        in content
    )


def test_lint_no_secrets() -> None:
    """Verify lint.yaml thin caller has no secrets block."""
    content = generate_thin_caller("lint.yaml")
    assert "secrets:" not in content


def test_custom_version() -> None:
    """Verify custom version in uses reference."""
    content = generate_thin_caller("lint.yaml", version="v5.8.0")
    assert f"{DEFAULT_REPO}/.github/workflows/lint.yaml@v5.8.0" in content


def test_custom_repo() -> None:
    """Verify custom repo in uses reference."""
    content = generate_thin_caller("lint.yaml", repo="myorg/myworkflows")
    assert "myorg/myworkflows/.github/workflows/lint.yaml@main" in content


def test_non_reusable_raises() -> None:
    """Raise ValueError for non-reusable workflow."""
    with pytest.raises(ValueError, match="workflow_call"):
        generate_thin_caller("tests.yaml")


def test_has_jobs_section() -> None:
    """Verify generated YAML has jobs section."""
    content = generate_thin_caller("lint.yaml")
    data = yaml.safe_load(content)
    assert "jobs" in data


def test_has_name() -> None:
    """Verify generated YAML has name field."""
    content = generate_thin_caller("lint.yaml")
    data = yaml.safe_load(content)
    assert "name" in data


def test_identifies_thin_caller(tmp_path: Path) -> None:
    """Identify a thin caller workflow."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        "---\nname: Lint\njobs:\n  lint:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/lint.yaml@v5.8.0\n",
        encoding="UTF-8",
    )
    result = identify_canonical_workflow(wf)
    assert result == "lint.yaml"


def test_returns_none_for_non_caller(tmp_path: Path) -> None:
    """Return None for non-caller workflow."""
    wf = tmp_path / "custom.yaml"
    wf.write_text(
        "---\nname: Custom\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo hello\n",
        encoding="UTF-8",
    )
    result = identify_canonical_workflow(wf)
    assert result is None


def test_returns_none_for_invalid_yaml(tmp_path: Path) -> None:
    """Return None for invalid YAML file."""
    wf = tmp_path / "bad.yaml"
    wf.write_text("{{invalid yaml", encoding="UTF-8")
    result = identify_canonical_workflow(wf)
    assert result is None


def test_returns_none_for_missing_file(tmp_path: Path) -> None:
    """Return None for missing file."""
    wf = tmp_path / "missing.yaml"
    result = identify_canonical_workflow(wf)
    assert result is None


def test_identify_custom_repo(tmp_path: Path) -> None:
    """Match with custom repo."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        "---\nname: Lint\njobs:\n  lint:\n"
        "    uses: myorg/myrepo/.github/workflows/lint.yaml@v1.0\n",
        encoding="UTF-8",
    )
    result = identify_canonical_workflow(wf, repo="myorg/myrepo")
    assert result == "lint.yaml"


def test_has_dispatch(tmp_path: Path) -> None:
    """Pass when workflow_dispatch is present."""
    wf = tmp_path / "test.yaml"
    wf.write_text(
        '---\n"on":\n  workflow_dispatch:\n  push:\n',
        encoding="UTF-8",
    )
    result = check_has_workflow_dispatch(wf)
    assert result.is_issue is False


def test_missing_dispatch(tmp_path: Path) -> None:
    """Fail when workflow_dispatch is missing."""
    wf = tmp_path / "test.yaml"
    wf.write_text('---\n"on":\n  push:\n', encoding="UTF-8")
    result = check_has_workflow_dispatch(wf)
    assert result.is_issue is True
    assert "missing" in result.message


def test_invalid_yaml(tmp_path: Path) -> None:
    """Error on invalid YAML."""
    wf = tmp_path / "bad.yaml"
    wf.write_text("{{invalid", encoding="UTF-8")
    result = check_has_workflow_dispatch(wf)
    assert result.is_issue is True
    assert result.level == AnnotationLevel.ERROR


def test_pinned_version(tmp_path: Path) -> None:
    """Pass when using a version tag."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        f"    uses: {DEFAULT_REPO}/.github/workflows/lint.yaml@v5.8.0\n",
        encoding="UTF-8",
    )
    result = check_version_pinned(wf)
    assert result.is_issue is False


def test_uses_main(tmp_path: Path) -> None:
    """Fail when using @main."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        f"    uses: {DEFAULT_REPO}/.github/workflows/lint.yaml@main\n",
        encoding="UTF-8",
    )
    result = check_version_pinned(wf)
    assert result.is_issue is True
    assert "@main" in result.message


def test_matching_triggers(tmp_path: Path) -> None:
    """Pass when triggers match canonical."""
    # lint.yaml has: workflow_dispatch, push, pull_request.
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        '---\n"on":\n  workflow_dispatch:\n  push:\n  pull_request:\n',
        encoding="UTF-8",
    )
    result = check_triggers_match(wf, "lint.yaml")
    assert result.is_issue is False


def test_missing_trigger(tmp_path: Path) -> None:
    """Fail when a trigger is missing."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        '---\n"on":\n  push:\n',
        encoding="UTF-8",
    )
    result = check_triggers_match(wf, "lint.yaml")
    assert result.is_issue is True
    assert "missing triggers" in result.message


def test_explicit_secrets_passed(tmp_path: Path) -> None:
    """Pass when all required secrets are passed explicitly."""
    wf = tmp_path / "release.yaml"
    wf.write_text(
        "---\njobs:\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v5.8.0\n"
        "    secrets:\n"
        "      PYPI_TOKEN: ${{ secrets.PYPI_TOKEN }}\n"
        "      WORKFLOW_UPDATE_GITHUB_PAT: ${{ secrets.WORKFLOW_UPDATE_GITHUB_PAT }}\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "release.yaml")
    assert result.is_issue is False


def test_secrets_inherit_still_accepted(tmp_path: Path) -> None:
    """Pass when secrets: inherit is used (backwards-compatible)."""
    wf = tmp_path / "release.yaml"
    wf.write_text(
        "---\njobs:\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v5.8.0\n"
        "    secrets: inherit\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "release.yaml")
    assert result.is_issue is False


def test_missing_secrets(tmp_path: Path) -> None:
    """Fail when no secrets are passed but required."""
    wf = tmp_path / "release.yaml"
    wf.write_text(
        "---\njobs:\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v5.8.0\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "release.yaml")
    assert result.is_issue is True
    assert "secrets" in result.message


def test_partial_secrets_missing(tmp_path: Path) -> None:
    """Fail when only some required secrets are passed."""
    wf = tmp_path / "release.yaml"
    wf.write_text(
        "---\njobs:\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v5.8.0\n"
        "    secrets:\n"
        "      PYPI_TOKEN: ${{ secrets.PYPI_TOKEN }}\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "release.yaml")
    assert result.is_issue is True
    assert "WORKFLOW_UPDATE_GITHUB_PAT" in result.message


def test_no_secrets_needed(tmp_path: Path) -> None:
    """Pass when canonical workflow has no secrets."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        "---\njobs:\n  lint:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/lint.yaml@v5.8.0\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "lint.yaml")
    assert result.is_issue is False


def test_clean_directory(tmp_path: Path) -> None:
    """Return 0 for clean workflows."""
    wf = tmp_path / "test.yaml"
    wf.write_text(
        '---\n"on":\n  workflow_dispatch:\n  push:\njobs:\n'
        "  build:\n    runs-on: ubuntu-latest\n",
        encoding="UTF-8",
    )
    exit_code = run_workflow_lint(tmp_path)
    assert exit_code == 0


def test_missing_directory(tmp_path: Path) -> None:
    """Return 1 for missing directory."""
    exit_code = run_workflow_lint(tmp_path / "nonexistent")
    assert exit_code == 1


def test_empty_directory(tmp_path: Path) -> None:
    """Return 0 for empty directory."""
    exit_code = run_workflow_lint(tmp_path)
    assert exit_code == 0


def test_warning_mode(tmp_path: Path) -> None:
    """Return 0 in warning mode even with issues."""
    wf = tmp_path / "test.yaml"
    wf.write_text('---\n"on":\n  push:\n', encoding="UTF-8")
    exit_code = run_workflow_lint(tmp_path, fatal=False)
    assert exit_code == 0


def test_fatal_mode(tmp_path: Path) -> None:
    """Return 1 in fatal mode when issues found."""
    wf = tmp_path / "test.yaml"
    wf.write_text('---\n"on":\n  push:\n', encoding="UTF-8")
    exit_code = run_workflow_lint(tmp_path, fatal=True)
    assert exit_code == 1


def test_create_thin_callers(tmp_path: Path) -> None:
    """Generate thin callers for all reusable workflows."""
    exit_code = generate_workflows(
        names=(),
        output_format=WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=False,
    )
    assert exit_code == 0
    for filename in REUSABLE_WORKFLOWS:
        assert (tmp_path / filename).exists()


def test_create_specific_workflow(tmp_path: Path) -> None:
    """Generate a single thin caller."""
    exit_code = generate_workflows(
        names=("lint.yaml",),
        output_format=WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        version="v5.8.0",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=False,
    )
    assert exit_code == 0
    assert (tmp_path / "lint.yaml").exists()
    content = (tmp_path / "lint.yaml").read_text(encoding="UTF-8")
    assert "v5.8.0" in content


def test_create_errors_if_exists(tmp_path: Path) -> None:
    """Error in create mode if file exists."""
    (tmp_path / "lint.yaml").write_text("existing", encoding="UTF-8")
    exit_code = generate_workflows(
        names=("lint.yaml",),
        output_format=WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=False,
    )
    assert exit_code == 1


def test_sync_overwrites(tmp_path: Path) -> None:
    """Overwrite in sync mode."""
    (tmp_path / "lint.yaml").write_text("old content", encoding="UTF-8")
    exit_code = generate_workflows(
        names=("lint.yaml",),
        output_format=WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 0
    content = (tmp_path / "lint.yaml").read_text(encoding="UTF-8")
    assert content != "old content"


def test_skip_non_reusable_thin_caller(tmp_path: Path) -> None:
    """Skip non-reusable workflows in thin-caller mode."""
    exit_code = generate_workflows(
        names=("tests.yaml",),
        output_format=WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=False,
    )
    assert exit_code == 0
    assert not (tmp_path / "tests.yaml").exists()


def test_full_copy(tmp_path: Path) -> None:
    """Generate full copy of a workflow."""
    exit_code = generate_workflows(
        names=("lint.yaml",),
        output_format=WorkflowFormat.FULL_COPY,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=False,
    )
    assert exit_code == 0
    assert (tmp_path / "lint.yaml").exists()
    content = (tmp_path / "lint.yaml").read_text(encoding="UTF-8")
    # Full copy should contain the original workflow content.
    assert "jobs:" in content


def test_creates_output_dir(tmp_path: Path) -> None:
    """Create output directory if it doesn't exist."""
    output_dir = tmp_path / "sub" / "dir"
    exit_code = generate_workflows(
        names=("lint.yaml",),
        output_format=WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=output_dir,
        overwrite=False,
    )
    assert exit_code == 0
    assert (output_dir / "lint.yaml").exists()


def test_values() -> None:
    """Verify expected enum values."""
    assert WorkflowFormat.FULL_COPY == "full-copy"
    assert WorkflowFormat.HEADER_ONLY == "header-only"
    assert WorkflowFormat.SYMLINK == "symlink"
    assert WorkflowFormat.THIN_CALLER == "thin-caller"


def test_from_string() -> None:
    """Verify construction from string."""
    assert WorkflowFormat("full-copy") == WorkflowFormat.FULL_COPY
    assert WorkflowFormat("header-only") == WorkflowFormat.HEADER_ONLY
    assert WorkflowFormat("symlink") == WorkflowFormat.SYMLINK
    assert WorkflowFormat("thin-caller") == WorkflowFormat.THIN_CALLER


def test_default_level() -> None:
    """Verify default annotation level is WARNING."""
    result = LintResult(message="test", is_issue=True)
    assert result.level == AnnotationLevel.WARNING


def test_custom_level() -> None:
    """Verify custom annotation level."""
    result = LintResult(message="test", is_issue=True, level=AnnotationLevel.ERROR)
    assert result.level == AnnotationLevel.ERROR


# ---------------------------------------------------------------------------
# Concurrency extraction tests
# ---------------------------------------------------------------------------

WORKFLOWS_WITH_CONCURRENCY = (
    "autofix.yaml",
    "changelog.yaml",
    "debug.yaml",
    "docs.yaml",
    "labels.yaml",
    "lint.yaml",
    "release.yaml",
    "renovate.yaml",
)
"""Canonical workflows that define a concurrency block."""

WORKFLOWS_WITHOUT_CONCURRENCY = ("autolock.yaml", "cancel-runs.yaml")
"""Canonical workflows that do not define a concurrency block."""


@pytest.mark.parametrize("filename", WORKFLOWS_WITH_CONCURRENCY)
def test_concurrency_present(filename: str) -> None:
    """Verify concurrency is extracted for workflows that define it."""
    info = extract_trigger_info(filename)
    assert info.concurrency is not None
    assert info.raw_concurrency is not None
    assert "concurrency:" in info.raw_concurrency


@pytest.mark.parametrize("filename", WORKFLOWS_WITHOUT_CONCURRENCY)
def test_concurrency_absent(filename: str) -> None:
    """Verify concurrency is None for workflows without it."""
    info = extract_trigger_info(filename)
    assert info.concurrency is None
    assert info.raw_concurrency is None


def test_concurrency_preserves_expressions() -> None:
    """Verify raw concurrency preserves ``${{ }}`` expressions."""
    info = extract_trigger_info("lint.yaml")
    assert info.raw_concurrency is not None
    assert "${{" in info.raw_concurrency


def test_concurrency_preserves_comments() -> None:
    """Verify raw concurrency preserves inline comments."""
    info = extract_trigger_info("release.yaml")
    assert info.raw_concurrency is not None
    # release.yaml has explanatory comments in its concurrency block.
    assert "#" in info.raw_concurrency


# ---------------------------------------------------------------------------
# Thin caller omits concurrency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_thin_caller_omits_concurrency(filename: str) -> None:
    """Verify thin callers never include concurrency.

    The reusable workflow's own concurrency block applies when called via
    ``workflow_call``, so duplicating it in the thin caller is unnecessary.
    """
    content = generate_thin_caller(filename)
    assert "concurrency:" not in content


# ---------------------------------------------------------------------------
# Header generation tests
# ---------------------------------------------------------------------------


def test_header_extraction_tests_yaml() -> None:
    """Verify header extraction for tests.yaml."""
    header = generate_workflow_header("tests.yaml")
    assert "name:" in header
    assert "concurrency:" in header
    assert "jobs:" not in header


def test_header_extraction_lint_yaml() -> None:
    """Verify header extraction for lint.yaml."""
    header = generate_workflow_header("lint.yaml")
    assert "name:" in header
    assert '"on":' in header or "on:" in header
    assert "jobs:" not in header


def test_header_extraction_nonexistent() -> None:
    """Raise FileNotFoundError for missing workflow."""
    with pytest.raises(FileNotFoundError):
        generate_workflow_header("nonexistent.yaml")


# ---------------------------------------------------------------------------
# Header-only format tests
# ---------------------------------------------------------------------------


def test_header_only_syncs_header(tmp_path: Path) -> None:
    """Verify header-only syncs header and preserves downstream jobs."""
    target = tmp_path / "tests.yaml"
    target.write_text(
        '---\nname: Old Name\n"on":\n  push:\n\njobs:\n\n'
        "  my-tests:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo hello\n",
        encoding="UTF-8",
    )
    exit_code = generate_workflows(
        names=("tests.yaml",),
        output_format=WorkflowFormat.HEADER_ONLY,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 0
    content = target.read_text(encoding="UTF-8")
    # Header should come from canonical tests.yaml.
    assert "Tests" in content
    assert "concurrency:" in content
    # Downstream jobs section should be preserved.
    assert "my-tests:" in content
    assert "echo hello" in content


def test_header_only_errors_on_missing_file(tmp_path: Path) -> None:
    """Error when target file does not exist in header-only mode."""
    exit_code = generate_workflows(
        names=("tests.yaml",),
        output_format=WorkflowFormat.HEADER_ONLY,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 1


def test_header_only_errors_on_no_jobs(tmp_path: Path) -> None:
    """Error when target file has no jobs: line."""
    target = tmp_path / "tests.yaml"
    target.write_text("---\nname: No Jobs\n", encoding="UTF-8")
    exit_code = generate_workflows(
        names=("tests.yaml",),
        output_format=WorkflowFormat.HEADER_ONLY,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 1


def test_header_only_defaults_to_non_reusable(tmp_path: Path) -> None:
    """Verify header-only defaults to non-reusable workflows."""
    # Create target files for non-reusable workflows.
    for filename in NON_REUSABLE_WORKFLOWS:
        target = tmp_path / filename
        target.write_text(
            '---\nname: Old\n"on":\n  push:\n\njobs:\n\n'
            "  test:\n    runs-on: ubuntu-latest\n",
            encoding="UTF-8",
        )
    exit_code = generate_workflows(
        names=(),
        output_format=WorkflowFormat.HEADER_ONLY,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 0
    for filename in NON_REUSABLE_WORKFLOWS:
        content = (tmp_path / filename).read_text(encoding="UTF-8")
        assert "concurrency:" in content


# ---------------------------------------------------------------------------
# Config filtering tests (_apply_workflow_config)
# ---------------------------------------------------------------------------


def test_apply_config_explicit_names_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit CLI args bypass config filtering entirely."""
    pyproject_content = """\
[tool.repomatic]
workflow-sync = false
workflow-sync-exclude = ["lint.yaml"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    monkeypatch.chdir(tmp_path)

    result = _apply_workflow_config(
        ("lint.yaml", "release.yaml"),
        WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
    )
    assert result == ("lint.yaml", "release.yaml")


def test_apply_config_no_explicit_names_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without config, returns format-specific defaults."""
    monkeypatch.chdir(tmp_path)

    result = _apply_workflow_config(
        (),
        WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
    )
    assert result == REUSABLE_WORKFLOWS

    result = _apply_workflow_config(
        (),
        WorkflowFormat.HEADER_ONLY,  # type: ignore[arg-type]
    )
    assert result == tuple(sorted(NON_REUSABLE_WORKFLOWS))


def test_apply_config_global_toggle_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returns None when workflow-sync is false and no explicit args."""
    pyproject_content = """\
[tool.repomatic]
workflow-sync = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    monkeypatch.chdir(tmp_path)

    result = _apply_workflow_config(
        (),
        WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
    )
    assert result is None


def test_apply_config_excludes_thin_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclude list removes workflows from thin-caller defaults."""
    pyproject_content = """\
[tool.repomatic]
workflow-sync-exclude = ["debug.yaml"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    monkeypatch.chdir(tmp_path)

    result = _apply_workflow_config(
        (),
        WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
    )
    assert result is not None
    assert "debug.yaml" not in result
    # Other reusable workflows are still present.
    assert "lint.yaml" in result
    assert "release.yaml" in result


def test_apply_config_excludes_header_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclude list removes workflows from header-only defaults."""
    pyproject_content = """\
[tool.repomatic]
workflow-sync-exclude = ["tests.yaml"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    monkeypatch.chdir(tmp_path)

    result = _apply_workflow_config(
        (),
        WorkflowFormat.HEADER_ONLY,  # type: ignore[arg-type]
    )
    assert result is not None
    assert "tests.yaml" not in result


def test_apply_config_warns_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Warning logged for unknown workflow in exclude list."""
    pyproject_content = """\
[tool.repomatic]
workflow-sync-exclude = ["nonexistent.yaml"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    monkeypatch.chdir(tmp_path)

    import logging

    with caplog.at_level(logging.WARNING):
        result = _apply_workflow_config(
            (),
            WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        )

    assert result is not None
    assert "nonexistent.yaml" in caplog.text
    assert "Unknown workflow" in caplog.text


def test_generate_with_exclude_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: excluded workflow is not created."""
    pyproject_content = """\
[tool.repomatic]
workflow-sync-exclude = ["debug.yaml"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    monkeypatch.chdir(tmp_path)

    filtered = _apply_workflow_config(
        (),
        WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
    )
    assert filtered is not None

    output_dir = tmp_path / ".github" / "workflows"
    exit_code = generate_workflows(
        names=filtered,
        output_format=WorkflowFormat.THIN_CALLER,  # type: ignore[arg-type]
        version="main",
        repo=DEFAULT_REPO,
        output_dir=output_dir,
        overwrite=False,
    )
    assert exit_code == 0
    assert not (output_dir / "debug.yaml").exists()
    # Other workflows are created.
    assert (output_dir / "lint.yaml").exists()
    assert (output_dir / "release.yaml").exists()
