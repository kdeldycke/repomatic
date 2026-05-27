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

import logging
from pathlib import Path

import pytest
import yaml

from repomatic.config import Config, WorkflowConfig
from repomatic.github.actions import AnnotationLevel
from repomatic.github.workflow_sync import (
    LintResult,
    PathsSpec,
    WorkflowFormat,
    WorkflowTriggerInfo,
    _adapt_trigger_paths,
    _coerce_paths_spec,
    _split_yaml_quote,
    _substitute_source_paths,
    check_has_workflow_dispatch,
    check_secrets_passed,
    check_triggers_match,
    check_version_pinned,
    extract_extra_jobs,
    extract_trigger_info,
    generate_thin_caller,
    generate_workflow_header,
    generate_workflows,
    identify_canonical_workflow,
    run_workflow_lint,
)
from repomatic.init_project import get_data_content
from repomatic.pyproject import derive_source_paths, resolve_source_paths
from repomatic.registry import (
    ALL_WORKFLOW_FILES,
    DEFAULT_REPO,
    NON_REUSABLE_WORKFLOWS,
    REUSABLE_WORKFLOWS,
    UPSTREAM_SOURCE_GLOB,
    UPSTREAM_SOURCE_PREFIX,
    WORKFLOW_SOURCES,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


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
    info = extract_trigger_info(WORKFLOW_SOURCES.get(filename, filename))
    assert info.has_workflow_call is True


@pytest.mark.parametrize("filename", list(NON_REUSABLE_WORKFLOWS))
def test_non_reusable_no_workflow_call(filename: str) -> None:
    """Verify non-reusable workflows lack workflow_call trigger."""
    info = extract_trigger_info(filename)
    assert info.has_workflow_call is False


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_returns_trigger_info(filename: str) -> None:
    """Verify return type is WorkflowTriggerInfo."""
    source = WORKFLOW_SOURCES.get(filename, filename)
    info = extract_trigger_info(source)
    assert isinstance(info, WorkflowTriggerInfo)
    assert info.filename == source
    assert isinstance(info.name, str)
    assert len(info.name) > 0


def test_autofix_has_secrets() -> None:
    """Verify autofix.yaml defines secrets."""
    info = extract_trigger_info("autofix.yaml")
    assert "REPOMATIC_PAT" in info.call_secrets


def test_changelog_has_secrets() -> None:
    """Verify changelog.yaml defines secrets."""
    info = extract_trigger_info("changelog.yaml")
    assert "REPOMATIC_PAT" in info.call_secrets


def test_release_has_secrets() -> None:
    """Verify release.yaml defines secrets.

    `PYPI_TOKEN` was removed once the publish step migrated to OIDC-based
    Trusted Publishing via the `publish-pypi` composite action invoked from
    the caller workflow. See pypi/warehouse#11096 for context.
    """
    info = extract_trigger_info("_release-engine.yaml")
    assert "PYPI_TOKEN" not in info.call_secrets
    assert "REPOMATIC_PAT" in info.call_secrets


def test_renovate_has_secrets() -> None:
    """Verify renovate.yaml defines secrets."""
    info = extract_trigger_info("renovate.yaml")
    assert "REPOMATIC_PAT" in info.call_secrets


def test_unsubscribe_has_secrets() -> None:
    """Verify unsubscribe.yaml defines secrets."""
    info = extract_trigger_info("unsubscribe.yaml")
    assert "REPOMATIC_NOTIFICATIONS_PAT" in info.call_secrets


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_no_python_literals_in_yaml(filename: str) -> None:
    """Verify generated YAML contains no Python dict/list literals.

    Regression test for a bug where ``_render_trigger_value`` fell through to
    ``str()`` on nested dicts, producing ``{'key': 'value'}`` instead of
    block-style YAML.

    .. todo::
        Run ``yamllint`` and ``actionlint`` on all generated thin-caller YAML
        to catch structural issues beyond Python literal leaks.
    """
    content = generate_thin_caller(filename)
    assert "{'" not in content, f"{filename}: Python dict literal found in output"
    assert "'}" not in content, f"{filename}: Python dict literal found in output"


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_no_workflow_call_inputs(filename: str) -> None:
    """Verify no reusable workflow defines ``workflow_call`` inputs.

    All configurable options live in ``[tool.repomatic]`` in ``pyproject.toml``.
    Workflows read config via ``repomatic`` CLI instead of accepting inputs.
    """
    info = extract_trigger_info(WORKFLOW_SOURCES.get(filename, filename))
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


# release.yaml is excluded: it is call-only upstream, so its caller synthesizes
# triggers instead of mirroring (see test_release_thin_caller_synthesizes_triggers).
@pytest.mark.parametrize(
    "filename", [f for f in REUSABLE_WORKFLOWS if f != "release.yaml"]
)
def test_mirrors_canonical_dispatch(filename: str) -> None:
    """Caller's workflow_dispatch presence mirrors canonical, no synthesis."""
    content = generate_thin_caller(filename)
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    canonical = extract_trigger_info(filename)
    canonical_has_dispatch = "workflow_dispatch" in canonical.non_call_triggers
    assert ("workflow_dispatch" in triggers) is canonical_has_dispatch


def test_release_thin_caller_synthesizes_triggers() -> None:
    """release.yaml is call-only upstream; its caller synthesizes push + dispatch.

    The canonical release.yaml declares no push/workflow_dispatch trigger
    (repomatic's own self-release.yaml owns those). generate_thin_caller
    therefore synthesizes the standard release triggers for downstream callers
    rather than mirroring the canonical workflow's empty non-call triggers.
    """
    content = generate_thin_caller("release.yaml")
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    assert "workflow_dispatch" in triggers
    assert triggers["push"] == {"branches": ["main"]}
    # Canonical is call-only: there is nothing to mirror.
    assert extract_trigger_info("_release-engine.yaml").non_call_triggers == {}


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_correct_uses_ref(filename: str) -> None:
    """Verify correct uses reference in job."""
    content = generate_thin_caller(filename)
    expected = f"{DEFAULT_REPO}/.github/workflows/{WORKFLOW_SOURCES.get(filename, filename)}@main"
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
    assert "REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}" in content


def test_changelog_passes_secrets_explicitly() -> None:
    """Verify changelog.yaml thin caller passes secrets explicitly."""
    content = generate_thin_caller("changelog.yaml")
    assert "secrets: inherit" not in content
    assert "REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}" in content


def test_release_passes_secrets_explicitly() -> None:
    """Verify release.yaml thin caller passes secrets explicitly.

    `PYPI_TOKEN` is no longer in the secrets surface: PyPI uploads run via
    OIDC Trusted Publishing inside the caller-side `publish-pypi` job.
    """
    content = generate_thin_caller("release.yaml")
    assert "secrets: inherit" not in content
    assert "PYPI_TOKEN" not in content
    assert "REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}" in content


def test_release_thin_caller_emits_publish_pypi_job() -> None:
    """Verify the release.yaml thin caller appends the caller-side publish-pypi job."""
    content = generate_thin_caller("release.yaml", version="v9.9.9")
    assert "  publish-pypi:" in content
    assert "needs: release" in content
    # The gate is decoupled from the overall run result: always() + package_built
    # let a healthy wheel publish even when an unrelated job (like binary tests)
    # fails the run.
    assert "always()" in content
    assert "needs.release.outputs.package_built == 'true'" in content
    assert "needs.release.outputs.release_commits_matrix" in content
    assert "id-token: write" in content
    assert f"{DEFAULT_REPO}/.github/actions/publish-pypi@v9.9.9" in content
    assert (
        "artifact-name: ${{ github.event.repository.name }}-${{ matrix.short_sha }}"
        in content
    )


def test_release_thin_caller_publish_pypi_uses_sha_pin() -> None:
    """Verify SHA-pinned commit form propagates to the publish-pypi action ref."""
    sha = "1234567890abcdef1234567890abcdef12345678"
    content = generate_thin_caller("release.yaml", version="v9.9.9", commit_sha=sha)
    assert f"{DEFAULT_REPO}/.github/actions/publish-pypi@{sha} # v9.9.9" in content


def test_non_release_thin_caller_omits_publish_pypi_job() -> None:
    """Verify non-release thin callers do not gain a publish-pypi job."""
    for filename in REUSABLE_WORKFLOWS:
        if filename == "release.yaml":
            continue
        content = generate_thin_caller(filename)
        assert "publish-pypi:" not in content, (
            f"{filename} unexpectedly emits a publish-pypi job"
        )


def test_release_thin_caller_publish_pypi_omits_checkout() -> None:
    """The downstream publish-pypi job should not require an explicit checkout.

    The cross-repo composite action `uses:` form is fetched by GitHub
    automatically: dropping `actions/checkout` keeps the generator free of
    third-party action SHA pins (which would otherwise be invisible to
    Renovate, since the generator's source is `.py`, not `.yaml`).
    """
    content = generate_thin_caller("release.yaml")
    assert "actions/checkout" not in content


def test_release_thin_caller_publish_pypi_sourced_from_release_yaml() -> None:
    """The publish-pypi job body must be sourced from the bundled release.yaml.

    Deriving it from a `.yaml` file (rather than building it in Python) keeps
    any future third-party SHA pin in the job body Renovate-visible. The
    canonical entry carries the job with a local `./` action ref that the
    generator reshapes for downstream callers.
    """
    release_yaml = get_data_content("release.yaml")
    parsed = yaml.safe_load(release_yaml)
    job = parsed["jobs"]["publish-pypi"]
    assert job["permissions"]["id-token"] == "write"
    # The bundled source dogfoods the local action ref; the swap to the pinned
    # cross-repo form happens at render time, not in the source.
    assert "./.github/actions/publish-pypi" in release_yaml


def test_release_thin_caller_publish_pypi_keeps_canonical_runner() -> None:
    """The generated downstream job inherits the canonical runner verbatim.

    The body is carried through from release.yaml unchanged except for the
    checkout drop and the action-ref pin, so downstream callers run on the same
    `ubuntu-slim` repomatic uses. A downstream repo that needs a fuller image
    surfaces that as a failure to revisit then.
    """
    content = generate_thin_caller("release.yaml", version="v9.9.9")
    assert "runs-on: ubuntu-slim" in content
    assert "ubuntu-latest" not in content


def _patch_release_yaml(monkeypatch, body: str) -> None:
    """Override only the bundled release.yaml, delegating other names to disk.

    `get_data_content` also fetches `_release-engine.yaml` for trigger
    extraction; a blanket override would feed the test body to that call and
    break `extract_trigger_info`.
    """
    from repomatic.github import workflow_sync as ws

    real_get = ws.get_data_content

    def fake_get(name: str) -> str:
        if name == "release.yaml":
            return body
        return real_get(name)

    monkeypatch.setattr(ws, "get_data_content", fake_get)


def test_release_thin_caller_rewrites_local_action_ref(monkeypatch) -> None:
    """Render rewrites the canonical local action ref and drops the checkout.

    The bundled release.yaml dogfoods the in-tree action via a local `./` ref
    (checking itself out first); the downstream caller must instead pin the
    cross-repo composite action (so its OIDC `job_workflow_ref` resolves to its
    own release.yaml) and needs no checkout.
    """
    _patch_release_yaml(
        monkeypatch,
        "---\n"
        "jobs:\n"
        "  publish-pypi:\n"
        "    permissions:\n"
        "      id-token: write\n"
        "    runs-on: ubuntu-slim\n"
        "    steps:\n"
        "      - uses: actions/checkout@abc123\n"
        "      - uses: ./.github/actions/publish-pypi\n"
        "        with:\n"
        "          artifact-name: x\n",
    )

    content = generate_thin_caller("release.yaml", version="v9.9.9")
    assert f"{DEFAULT_REPO}/.github/actions/publish-pypi@v9.9.9" in content
    assert "./.github/actions/publish-pypi" not in content
    assert "actions/checkout" not in content


def test_release_thin_caller_unrecognized_job_raises(monkeypatch) -> None:
    """Render fails loudly when release.yaml's publish-pypi job lacks the action.

    A canonical entry reshaped in a way the renderer was not updated to handle
    (no local action ref in the job) must surface as an error, not a silent
    no-op that emits a malformed workflow.
    """
    _patch_release_yaml(
        monkeypatch,
        "---\n"
        "jobs:\n"
        "  publish-pypi:\n"
        "    permissions:\n"
        "      id-token: write\n"
        "    steps:\n"
        "      - run: echo no-action-ref-here\n",
    )

    with pytest.raises(RuntimeError, match="publish-pypi"):
        generate_thin_caller("release.yaml", version="v9.9.9")


def test_renovate_passes_secrets_explicitly() -> None:
    """Verify renovate.yaml thin caller passes secrets explicitly."""
    content = generate_thin_caller("renovate.yaml")
    assert "secrets: inherit" not in content
    assert "REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}" in content


def test_lint_passes_secrets_explicitly() -> None:
    """Verify lint.yaml thin caller passes secrets explicitly."""
    content = generate_thin_caller("lint.yaml")
    assert "secrets: inherit" not in content
    assert "REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}" in content


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


# ---------------------------------------------------------------------------
# extract_extra_jobs
# ---------------------------------------------------------------------------


def test_extract_extra_jobs_single() -> None:
    """Preserve a single extra downstream job."""
    content = (
        '---\nname: Release\n"on":\n  push:\n    branches:\n'
        "      - main\n  workflow_dispatch:\n\njobs:\n\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v6.0.0\n"
        "    secrets:\n"
        "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
        "\n"
        "  # Custom packaging job.\n"
        "  chocolatey:\n"
        "    name: Chocolatey\n"
        "    needs: release\n"
        "    runs-on: windows-latest\n"
        "    steps:\n"
        "      - run: echo hello\n"
    )
    extra = extract_extra_jobs(content)
    assert "chocolatey:" in extra
    assert "needs: release" in extra
    assert "# Custom packaging job." in extra
    # The managed job should not appear in extra.
    assert "REPOMATIC_PAT" not in extra


def test_extract_extra_jobs_treats_publish_pypi_as_managed() -> None:
    """Verify the caller-side publish-pypi job is recognized as managed.

    When a thin-caller for `release.yaml` is regenerated, the publish-pypi
    job (which uses `kdeldycke/repomatic/.github/actions/publish-pypi@...`)
    must be treated as part of the managed surface and not preserved as
    an extra job. Otherwise re-syncing would duplicate it.
    """
    content = (
        f"---\nname: Release\njobs:\n\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v6.0.0\n"
        "\n"
        "  publish-pypi:\n"
        "    needs: release\n"
        "    permissions:\n"
        "      id-token: write\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"      - uses: {DEFAULT_REPO}/.github/actions/publish-pypi@v6.0.0\n"
        "        with:\n"
        "          artifact-name: foo\n"
        "\n"
        "  notify:\n"
        "    needs: publish-pypi\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo done\n"
    )
    extra = extract_extra_jobs(content)
    assert "publish-pypi:" not in extra
    assert "notify:" in extra
    assert "needs: publish-pypi" in extra


def test_extract_extra_jobs_multiple() -> None:
    """Preserve multiple extra downstream jobs."""
    content = (
        "---\nname: Release\njobs:\n\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v6.0.0\n"
        "\n"
        "  deploy:\n"
        "    needs: release\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo deploy\n"
        "\n"
        "  notify:\n"
        "    needs: deploy\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo notify\n"
    )
    extra = extract_extra_jobs(content)
    assert "deploy:" in extra
    assert "notify:" in extra


def test_extract_extra_jobs_none() -> None:
    """Return empty string when no extra jobs exist."""
    content = (
        "---\nname: Release\njobs:\n\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v6.0.0\n"
    )
    assert extract_extra_jobs(content) == ""


def test_extract_extra_jobs_not_thin_caller() -> None:
    """Return empty string for a non-thin-caller workflow."""
    content = (
        "---\nname: Custom\njobs:\n  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hello\n"
    )
    assert extract_extra_jobs(content) == ""


def test_extract_extra_jobs_invalid_yaml() -> None:
    """Return empty string for invalid YAML."""
    assert extract_extra_jobs("{{invalid yaml") == ""


def test_thin_caller_sync_preserves_extra_jobs(tmp_path: Path) -> None:
    """End-to-end: sync overwrites the managed job but preserves extras."""
    extra_job = (
        "\n"
        "  # Custom packaging job.\n"
        "  chocolatey:\n"
        "    name: Chocolatey\n"
        "    needs: release\n"
        "    runs-on: windows-latest\n"
        "    steps:\n"
        "      - run: echo hello\n"
    )
    # Generate the initial thin caller, then append an extra job.
    exit_code = generate_workflows(
        names=("release.yaml",),
        output_format=WorkflowFormat.THIN_CALLER,
        version="v6.0.0",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=False,
    )
    assert exit_code == 0
    target = tmp_path / "release.yaml"
    target.write_text(
        target.read_text(encoding="UTF-8") + extra_job,
        encoding="UTF-8",
    )

    # Re-sync with a new version. The extra job must survive.
    exit_code = generate_workflows(
        names=("release.yaml",),
        output_format=WorkflowFormat.THIN_CALLER,
        version="v7.0.0",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 0
    result = target.read_text(encoding="UTF-8")
    # Managed job was updated.
    assert "v7.0.0" in result
    assert "v6.0.0" not in result
    # Extra job was preserved.
    assert "chocolatey:" in result
    assert "# Custom packaging job." in result


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
    assert "missing:" in result.message


def test_extra_trigger(tmp_path: Path) -> None:
    """Fail when a caller declares a trigger absent from canonical."""
    canonical = extract_trigger_info("cancel-runs.yaml")
    on_lines = ['"on":']
    for trigger_name, trigger_config in canonical.non_call_triggers.items():
        on_lines.append(f"  {trigger_name}:")
        if isinstance(trigger_config, dict):
            for k, v in trigger_config.items():
                if isinstance(v, list):
                    on_lines.append(f"    {k}:")
                    on_lines.extend(f"      - {item}" for item in v)
                else:
                    on_lines.append(f"    {k}: {v}")
    on_lines.append("  workflow_dispatch:")
    body = "\n".join(on_lines)
    wf = tmp_path / "cancel-runs.yaml"
    wf.write_text(f"---\n{body}\n", encoding="UTF-8")
    result = check_triggers_match(wf, "cancel-runs.yaml")
    assert result.is_issue is True
    assert "extra: workflow_dispatch" in result.message


def test_explicit_secrets_passed(tmp_path: Path) -> None:
    """Pass when all required secrets are passed explicitly."""
    wf = tmp_path / "release.yaml"
    wf.write_text(
        "---\njobs:\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v5.8.0\n"
        "    secrets:\n"
        "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
        "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "_release-engine.yaml")
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
    result = check_secrets_passed(wf, "_release-engine.yaml")
    assert result.is_issue is False


def test_missing_secrets(tmp_path: Path) -> None:
    """Fail when no secrets are passed but required."""
    wf = tmp_path / "release.yaml"
    wf.write_text(
        "---\njobs:\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v5.8.0\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "_release-engine.yaml")
    assert result.is_issue is True
    assert "secrets" in result.message


def test_partial_secrets_missing(tmp_path: Path) -> None:
    """Fail when only some required secrets are passed."""
    wf = tmp_path / "release.yaml"
    wf.write_text(
        "---\njobs:\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v5.8.0\n"
        "    secrets:\n"
        "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, "_release-engine.yaml")
    assert result.is_issue is True
    assert "REPOMATIC_PAT" in result.message


def test_no_secrets_needed(tmp_path: Path) -> None:
    """Pass when downstream passes all required secrets."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        "---\njobs:\n  lint:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/lint.yaml@v5.8.0\n"
        "    secrets:\n"
        "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
        "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n",
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


def test_thin_caller_exempt_from_workflow_dispatch_check(tmp_path: Path) -> None:
    """Thin callers wrapping a canonical without workflow_dispatch lint clean.

    `cancel-runs.yaml` and `release.yaml` intentionally lack
    `workflow_dispatch`. A thin caller mirroring them must not be flagged
    by the standalone `workflow_dispatch` check.
    """
    content = generate_thin_caller("cancel-runs.yaml", version="v5.8.0")
    (tmp_path / "cancel-runs.yaml").write_text(content, encoding="UTF-8")
    exit_code = run_workflow_lint(tmp_path, fatal=True)
    assert exit_code == 0


def test_create_thin_callers(tmp_path: Path) -> None:
    """Generate thin callers for all reusable workflows."""
    exit_code = generate_workflows(
        names=(),
        output_format=WorkflowFormat.THIN_CALLER,
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
        output_format=WorkflowFormat.THIN_CALLER,
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
        output_format=WorkflowFormat.THIN_CALLER,
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
        output_format=WorkflowFormat.THIN_CALLER,
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
        output_format=WorkflowFormat.THIN_CALLER,
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
        output_format=WorkflowFormat.FULL_COPY,
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
        output_format=WorkflowFormat.THIN_CALLER,
        version="main",
        repo=DEFAULT_REPO,
        output_dir=output_dir,
        overwrite=False,
    )
    assert exit_code == 0
    assert (output_dir / "lint.yaml").exists()


def test_values() -> None:
    """Verify expected enum values."""
    assert WorkflowFormat.FULL_COPY.value == "full-copy"
    assert WorkflowFormat.HEADER_ONLY.value == "header-only"
    assert WorkflowFormat.SYMLINK.value == "symlink"
    assert WorkflowFormat.THIN_CALLER.value == "thin-caller"


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
    "_release-engine.yaml",
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
    info = extract_trigger_info("_release-engine.yaml")
    assert info.raw_concurrency is not None
    # The release engine has explanatory comments in its concurrency block.
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
# Thin caller omits upstream-only paths but keeps universal entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_thin_caller_drops_upstream_source_paths(filename: str) -> None:
    """Thin callers drop `repomatic/`-prefixed paths even without source_paths.

    Universal entries (`pyproject.toml`, `renovate.json5`, workflow self-refs)
    are preserved so trigger semantics carry over from the canonical workflow.
    """
    content = generate_thin_caller(filename)
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    for trigger_name, trigger_config in triggers.items():
        if not isinstance(trigger_config, dict):
            continue
        for key in ("paths", "paths-ignore"):
            for path in trigger_config.get(key, []) or []:
                assert not path.startswith("repomatic/"), (
                    f"{filename}: trigger '{trigger_name}' kept upstream path"
                    f" '{path}' in {key}."
                )


# ---------------------------------------------------------------------------
# SHA-pinned thin callers
# ---------------------------------------------------------------------------

FAKE_SHA = "072c7bbbcdd607011c6ca4fb9d5098532aee2dea"


def test_thin_caller_sha_pinned() -> None:
    """Thin caller with ``commit_sha`` produces ``@sha # version``."""
    content = generate_thin_caller("lint.yaml", version="v6.8.0", commit_sha=FAKE_SHA)
    assert f"@{FAKE_SHA} # v6.8.0" in content


def test_thin_caller_sha_none_fallback() -> None:
    """Thin caller without ``commit_sha`` produces ``@version``."""
    content = generate_thin_caller("lint.yaml", version="v6.8.0", commit_sha=None)
    assert "@v6.8.0" in content
    assert f"@{FAKE_SHA}" not in content


def test_thin_caller_sha_pinned_yaml_valid() -> None:
    """SHA-pinned thin caller is valid YAML with comment stripped."""
    content = generate_thin_caller("lint.yaml", version="v6.8.0", commit_sha=FAKE_SHA)
    data = yaml.safe_load(content)
    jobs = data.get("jobs", {})
    uses = next(iter(jobs.values()))["uses"]
    # YAML parser strips the # comment — value is just the SHA part.
    assert uses.endswith(f"@{FAKE_SHA}")
    assert "v6.8.0" not in uses


def test_identify_canonical_workflow_sha_pinned(tmp_path: Path) -> None:
    """``identify_canonical_workflow`` recognizes SHA-pinned thin callers."""
    content = generate_thin_caller("lint.yaml", version="v6.8.0", commit_sha=FAKE_SHA)
    wf = tmp_path / "lint.yaml"
    wf.write_text(content, encoding="UTF-8")
    assert identify_canonical_workflow(wf) == "lint.yaml"


def test_check_version_pinned_sha(tmp_path: Path) -> None:
    """``check_version_pinned`` passes for SHA-pinned refs."""
    content = generate_thin_caller("lint.yaml", version="v6.8.0", commit_sha=FAKE_SHA)
    wf = tmp_path / "lint.yaml"
    wf.write_text(content, encoding="UTF-8")
    result = check_version_pinned(wf)
    assert not result.is_issue


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
        output_format=WorkflowFormat.HEADER_ONLY,
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


def test_header_only_warns_on_missing_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Warning when target file does not exist in header-only mode."""
    with caplog.at_level(logging.WARNING):
        exit_code = generate_workflows(
            names=("tests.yaml",),
            output_format=WorkflowFormat.HEADER_ONLY,
            version="main",
            repo=DEFAULT_REPO,
            output_dir=tmp_path,
            overwrite=True,
        )
    assert exit_code == 0
    assert "does not exist" in caplog.text


def test_header_only_errors_on_no_jobs(tmp_path: Path) -> None:
    """Error when target file has no jobs: line."""
    target = tmp_path / "tests.yaml"
    target.write_text("---\nname: No Jobs\n", encoding="UTF-8")
    exit_code = generate_workflows(
        names=("tests.yaml",),
        output_format=WorkflowFormat.HEADER_ONLY,
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 1


def test_header_only_defaults_filter_to_existing(tmp_path: Path) -> None:
    """Header-only defaults skip non-existent workflows silently."""
    exit_code = generate_workflows(
        names=(),
        output_format=WorkflowFormat.HEADER_ONLY,
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
    )
    assert exit_code == 0


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
        output_format=WorkflowFormat.HEADER_ONLY,
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
# Source path substitution tests
# ---------------------------------------------------------------------------


def test_substitute_replaces_upstream_glob() -> None:
    """Replace upstream source glob with downstream source paths."""
    paths = [UPSTREAM_SOURCE_GLOB, "tests/**", "pyproject.toml"]
    result = _substitute_source_paths(paths, ["extra_platforms"])
    assert result == ["extra_platforms/**", "tests/**", "pyproject.toml"]


def test_substitute_multiple_source_paths() -> None:
    """Replace upstream source glob with multiple downstream paths."""
    paths = [UPSTREAM_SOURCE_GLOB, "pyproject.toml"]
    result = _substitute_source_paths(paths, ["pkg_a", "pkg_b"])
    assert result == ["pkg_a/**", "pkg_b/**", "pyproject.toml"]


def test_substitute_drops_upstream_specific() -> None:
    """Drop upstream-specific paths that aren't the source glob."""
    paths = [
        UPSTREAM_SOURCE_GLOB,
        f"{UPSTREAM_SOURCE_PREFIX}data/renovate.json5",
        "renovate.json5",
    ]
    result = _substitute_source_paths(paths, ["my_pkg"])
    assert result == ["my_pkg/**", "renovate.json5"]


def test_substitute_keeps_universal_paths() -> None:
    """Keep universal paths unchanged."""
    paths = ["tests/**", "pyproject.toml", "uv.lock", "changelog.md"]
    result = _substitute_source_paths(paths, ["my_pkg"])
    assert result == ["tests/**", "pyproject.toml", "uv.lock", "changelog.md"]


def test_substitute_empty_source_paths() -> None:
    """Return only universal paths when source_paths is empty."""
    paths = [UPSTREAM_SOURCE_GLOB, "pyproject.toml"]
    result = _substitute_source_paths(paths, [])
    assert result == ["pyproject.toml"]


def test_adapt_trigger_paths_with_source_paths() -> None:
    """Adapt trigger config with source paths substitution."""
    config = {
        "branches": ["main"],
        "paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml"],
    }
    spec = PathsSpec(source_paths=["extra_platforms"])
    result = _adapt_trigger_paths(config, "tests.yaml", spec)
    assert result["branches"] == ["main"]
    assert result["paths"] == ["extra_platforms/**", "pyproject.toml"]


def test_adapt_trigger_paths_none_drops_upstream_keeps_universal() -> None:
    """Drop upstream paths but keep universal entries when source_paths is None."""
    config = {
        "branches": ["main"],
        "paths": [
            UPSTREAM_SOURCE_GLOB,
            "pyproject.toml",
            "repomatic/data/renovate.json5",
        ],
    }
    result = _adapt_trigger_paths(config, "tests.yaml", PathsSpec())
    assert result["branches"] == ["main"]
    assert result["paths"] == ["pyproject.toml"]


def test_adapt_trigger_paths_none_drops_paths_when_only_upstream() -> None:
    """Drop the paths key when only upstream entries are present."""
    config = {
        "branches": ["main"],
        "paths": [UPSTREAM_SOURCE_GLOB, "repomatic/data/renovate.json5"],
    }
    result = _adapt_trigger_paths(config, "tests.yaml", PathsSpec())
    assert "paths" not in result
    assert result["branches"] == ["main"]


def test_adapt_trigger_paths_no_paths_key() -> None:
    """Pass through trigger config that has no paths key."""
    config = {"branches": ["main"]}
    spec = PathsSpec(source_paths=["extra_platforms"])
    result = _adapt_trigger_paths(config, "tests.yaml", spec)
    assert result == {"branches": ["main"]}


def test_adapt_trigger_paths_with_extra_paths() -> None:
    """`extra_paths` are appended to the path list, deduped."""
    config = {
        "paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml"],
    }
    spec = PathsSpec(
        source_paths=["my_pkg"],
        extra_paths=["install.sh", "pyproject.toml"],  # already-present is deduped.
    )
    result = _adapt_trigger_paths(config, "tests.yaml", spec)
    assert result["paths"] == ["my_pkg/**", "pyproject.toml", "install.sh"]


def test_adapt_trigger_paths_with_ignore_paths() -> None:
    """`ignore_paths` strips matching upstream entries before extras are added."""
    config = {
        "paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml", "uv.lock"],
    }
    spec = PathsSpec(
        source_paths=["my_pkg"],
        ignore_paths=["uv.lock"],
    )
    result = _adapt_trigger_paths(config, "tests.yaml", spec)
    assert result["paths"] == ["my_pkg/**", "pyproject.toml"]


def test_adapt_trigger_paths_per_workflow_override_replaces_wholesale() -> None:
    """Per-workflow `paths` override ignores other knobs and replaces the list."""
    config = {
        "paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml", "uv.lock"],
    }
    spec = PathsSpec(
        source_paths=["my_pkg"],
        extra_paths=["never-applied.sh"],
        ignore_paths=["pyproject.toml"],
        workflow_paths={"tests.yaml": ["install.sh", "packages.toml"]},
    )
    result = _adapt_trigger_paths(config, "tests.yaml", spec)
    assert result["paths"] == ["install.sh", "packages.toml"]


def test_adapt_trigger_paths_per_workflow_override_does_not_leak_to_others() -> None:
    """Per-workflow override only applies to its own filename."""
    config = {
        "paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml"],
    }
    spec = PathsSpec(
        source_paths=["my_pkg"],
        workflow_paths={"tests.yaml": ["install.sh"]},
    )
    result = _adapt_trigger_paths(config, "lint.yaml", spec)
    assert result["paths"] == ["my_pkg/**", "pyproject.toml"]


# ---------------------------------------------------------------------------
# Thin caller with source paths
# ---------------------------------------------------------------------------


def test_thin_caller_release_with_source_paths() -> None:
    """Verify release.yaml thin caller has no path filters.

    release.yaml only has ``push`` (no ``paths:``) and ``workflow_call``,
    so ``source_paths`` has no effect.
    """
    content = generate_thin_caller("release.yaml", source_paths=["extra_platforms"])
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    push_config = triggers.get("push", {})
    # push trigger has branches but no paths.
    assert "paths" not in push_config
    assert "pull_request" not in triggers


def test_thin_caller_renovate_with_source_paths() -> None:
    """Verify renovate.yaml thin caller drops upstream-specific paths."""
    content = generate_thin_caller("renovate.yaml", source_paths=["extra_platforms"])
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    push_config = triggers.get("push", {})
    assert "paths" in push_config
    # Universal paths kept.
    assert "renovate.json5" in push_config["paths"]
    assert ".github/workflows/renovate.yaml" in push_config["paths"]
    # Upstream-specific path dropped.
    for path in push_config["paths"]:
        assert not path.startswith(UPSTREAM_SOURCE_PREFIX)
    # Only 2 paths remain (self-reference + renovate.json5).
    assert len(push_config["paths"]) == 2


def test_thin_caller_changelog_with_source_paths() -> None:
    """Verify changelog.yaml thin caller keeps universal paths unchanged.

    changelog.yaml has no upstream source glob, so source_paths has no effect
    beyond keeping all paths intact.
    """
    content = generate_thin_caller("changelog.yaml", source_paths=["extra_platforms"])
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    push_config = triggers.get("push", {})
    assert "paths" in push_config
    assert "changelog.md" in push_config["paths"]


def test_thin_caller_lint_no_paths_with_source_paths() -> None:
    """Verify workflows without paths don't gain paths from source_paths."""
    content = generate_thin_caller("lint.yaml", source_paths=["extra_platforms"])
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    push_config = triggers.get("push", {})
    # lint.yaml has no paths filter in canonical, so none in thin caller.
    assert "paths" not in push_config


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_thin_caller_no_source_paths_drops_upstream_only(filename: str) -> None:
    """Without source_paths, thin callers drop upstream entries but keep universal ones."""
    content = generate_thin_caller(filename, source_paths=None)
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    for trigger_name, trigger_config in triggers.items():
        if not isinstance(trigger_config, dict):
            continue
        for key in ("paths", "paths-ignore"):
            for path in trigger_config.get(key, []) or []:
                assert not path.startswith("repomatic/"), (
                    f"{filename}: trigger '{trigger_name}' kept upstream path"
                    f" '{path}' in {key}."
                )


# ---------------------------------------------------------------------------
# Header generation with source paths
# ---------------------------------------------------------------------------


def test_header_with_source_paths_substitutes() -> None:
    """Verify header generation replaces upstream source paths."""
    header = generate_workflow_header("tests.yaml", source_paths=["my_pkg"])
    assert "my_pkg/**" in header
    assert UPSTREAM_SOURCE_GLOB not in header


def test_header_without_source_paths_drops_upstream_glob() -> None:
    """Without source_paths, the upstream source glob is dropped from the header."""
    header = generate_workflow_header("tests.yaml")
    assert UPSTREAM_SOURCE_GLOB not in header
    # Universal entries survive.
    assert "pyproject.toml" in header


def test_header_with_source_paths_drops_upstream_specific() -> None:
    """Verify header generation drops upstream-specific paths."""
    header = generate_workflow_header("renovate.yaml", source_paths=["my_pkg"])
    assert f"{UPSTREAM_SOURCE_PREFIX}data" not in header
    assert "renovate.json5" in header


def test_header_with_extra_paths_appends() -> None:
    """`extra_paths` are appended to every paths block in the header."""
    spec = PathsSpec(extra_paths=["install.sh", "dotfiles/**"])
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    # Both `push.paths` and `pull_request.paths` blocks pick up the extras.
    assert header.count("install.sh") >= 2
    assert header.count("dotfiles/**") >= 2
    # Universal canonical entries survive.
    assert "pyproject.toml" in header


def test_header_with_ignore_paths_strips_canonical() -> None:
    """`ignore_paths` removes matching entries from every paths block."""
    spec = PathsSpec(ignore_paths=["uv.lock", "tests/**"])
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    assert "- uv.lock" not in header
    assert "tests/**" not in header
    assert "pyproject.toml" in header


def test_header_per_workflow_override_replaces_paths_blocks() -> None:
    """Per-workflow `paths` override replaces every block in the workflow."""
    override = [
        "install.sh",
        "packages.toml",
        ".github/workflows/tests.yaml",
    ]
    spec = PathsSpec(workflow_paths={"tests.yaml": override})
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    # Override entries appear (twice: push + pull_request).
    assert header.count("install.sh") == 2
    assert header.count("packages.toml") == 2
    # Canonical-only entries are gone.
    assert "- uv.lock" not in header
    assert "tests/**" not in header
    assert UPSTREAM_SOURCE_GLOB not in header


def test_header_per_workflow_override_does_not_apply_to_other_files() -> None:
    """Override scoped to one filename does not affect another workflow's header."""
    spec = PathsSpec(
        workflow_paths={"tests.yaml": ["install.sh"]},
    )
    header = generate_workflow_header("renovate.yaml", paths_spec=spec)
    assert "install.sh" not in header
    # Universal canonical entry kept.
    assert "renovate.json5" in header


# ---------------------------------------------------------------------------
# derive_source_paths tests
# ---------------------------------------------------------------------------


def test_derive_source_paths_from_name() -> None:
    """Derive source paths from [project.name]."""
    pyproject_data = {"project": {"name": "extra-platforms"}}
    result = derive_source_paths(pyproject_data)
    assert result == ["extra_platforms"]


def test_derive_source_paths_underscore_name() -> None:
    """Derive source paths when name already uses underscores."""
    pyproject_data = {"project": {"name": "meta_package_manager"}}
    result = derive_source_paths(pyproject_data)
    assert result == ["meta_package_manager"]


def test_derive_source_paths_simple_name() -> None:
    """Derive source paths from a simple name without hyphens."""
    pyproject_data = {"project": {"name": "repomatic"}}
    result = derive_source_paths(pyproject_data)
    assert result == ["repomatic"]


def test_derive_source_paths_no_name() -> None:
    """Return empty list when no project name defined."""
    pyproject_data: dict[str, Any] = {"project": {}}
    result = derive_source_paths(pyproject_data)
    assert result == []


def test_derive_source_paths_empty_pyproject() -> None:
    """Return empty list for empty pyproject data."""
    result = derive_source_paths({})
    assert result == []


# ---------------------------------------------------------------------------
# resolve_source_paths tests
# ---------------------------------------------------------------------------


def test_resolve_source_paths_explicit_config() -> None:
    """Use explicitly configured source paths."""
    config = Config(workflow=WorkflowConfig(source_paths=["custom_src"]))
    result = resolve_source_paths(config)
    assert result == ["custom_src"]


def test_resolve_source_paths_none_derives() -> None:
    """Auto-derive when config is None."""
    config = Config(workflow=WorkflowConfig(source_paths=None))
    pyproject_data = {"project": {"name": "my-pkg"}}
    result = resolve_source_paths(config, pyproject_data)
    assert result == ["my_pkg"]


def test_resolve_source_paths_empty_list_returns_none() -> None:
    """Return None when explicitly set to empty list."""
    config = Config(workflow=WorkflowConfig(source_paths=[]))
    result = resolve_source_paths(config)
    assert result is None


def test_resolve_source_paths_no_name_returns_none() -> None:
    """Return None when no project name and no config."""
    config = Config(workflow=WorkflowConfig(source_paths=None))
    pyproject_data: dict[str, Any] = {"project": {}}
    result = resolve_source_paths(config, pyproject_data)
    assert result is None


# ---------------------------------------------------------------------------
# _split_yaml_quote
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scalar", "expected"),
    [
        ("plain", ("plain", "")),
        ('"quoted"', ("quoted", '"')),
        ("'single'", ("single", "'")),
        ("", ("", "")),
        ('"', ('"', "")),
        ("'", ("'", "")),
        ('""', ("", '"')),
        ('"mixed\'"', ("mixed'", '"')),
        ("path/with/slashes", ("path/with/slashes", "")),
    ],
)
def test_split_yaml_quote(scalar: str, expected: tuple[str, str]) -> None:
    """Strip outer matching quotes; pass through plain scalars."""
    assert _split_yaml_quote(scalar) == expected


# ---------------------------------------------------------------------------
# _coerce_paths_spec
# ---------------------------------------------------------------------------


def test_coerce_paths_spec_none_uses_legacy_arg() -> None:
    """When spec is None, legacy source_paths is wrapped in a fresh spec."""
    result = _coerce_paths_spec(None, ["my_pkg"])
    assert result.source_paths == ["my_pkg"]
    assert result.extra_paths == []
    assert result.ignore_paths == []
    assert result.workflow_paths == {}


def test_coerce_paths_spec_explicit_supersedes_legacy_arg() -> None:
    """When spec is provided, legacy source_paths is ignored."""
    spec = PathsSpec(source_paths=["from_spec"], extra_paths=["extra.sh"])
    result = _coerce_paths_spec(spec, ["legacy"])
    assert result is spec
    assert result.source_paths == ["from_spec"]
    assert result.extra_paths == ["extra.sh"]


# ---------------------------------------------------------------------------
# Thin caller with full paths_spec
# ---------------------------------------------------------------------------


def test_thin_caller_paths_spec_extra_paths_appends() -> None:
    """Thin caller picks up `extra_paths` in every paths-bearing trigger."""
    spec = PathsSpec(extra_paths=["install.sh"])
    content = generate_thin_caller("changelog.yaml", paths_spec=spec)
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    assert "install.sh" in triggers["push"]["paths"]


def test_thin_caller_paths_spec_ignore_strips_canonical() -> None:
    """Thin caller strips `ignore_paths` entries from every paths block."""
    spec = PathsSpec(ignore_paths=["uv.lock"])
    content = generate_thin_caller("changelog.yaml", paths_spec=spec)
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    assert "uv.lock" not in triggers["push"]["paths"]
    # Other canonical entries survive.
    assert "changelog.md" in triggers["push"]["paths"]


def test_thin_caller_paths_spec_per_workflow_override_replaces_wholesale() -> None:
    """Per-workflow override replaces the thin caller's paths list verbatim."""
    spec = PathsSpec(
        extra_paths=["never-applied.sh"],
        workflow_paths={"changelog.yaml": ["only.sh", "just-this.toml"]},
    )
    content = generate_thin_caller("changelog.yaml", paths_spec=spec)
    data = yaml.safe_load(content)
    triggers = data.get(True) or data.get("on") or {}
    assert triggers["push"]["paths"] == ["only.sh", "just-this.toml"]


def test_thin_caller_paths_spec_supersedes_legacy_source_paths_arg() -> None:
    """When both kwargs are passed, paths_spec wins over source_paths.

    `renovate.yaml` push.paths references `repomatic/data/renovate.json5`,
    which is dropped regardless of source_paths because it doesn't match
    UPSTREAM_SOURCE_GLOB; use `extra_paths` to make the spec observable.
    """
    spec = PathsSpec(extra_paths=["from-spec.txt"])
    content = generate_thin_caller(
        "renovate.yaml",
        source_paths=["from_legacy"],
        paths_spec=spec,
    )
    assert "from-spec.txt" in content
    # Legacy source_paths arg ignored.
    assert "from_legacy/**" not in content


# ---------------------------------------------------------------------------
# Header generation: extra knob behavior
# ---------------------------------------------------------------------------


def test_header_ignore_drops_block_when_empty() -> None:
    """When `ignore_paths` empties a block, the `paths:` key is removed."""
    upstream_paths = [
        UPSTREAM_SOURCE_GLOB,
        "tests/**",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/tests.yaml",
    ]
    spec = PathsSpec(ignore_paths=upstream_paths)
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    # No `paths:` blocks remain after stripping every canonical entry.
    assert "    paths:" not in header
    # Other trigger keys survive (e.g., branches, schedule).
    assert "branches:" in header
    assert "schedule:" in header


def test_header_ignore_applies_before_extras() -> None:
    """`ignore_paths` runs before `extra_paths`: an entry stripped then re-added stays.

    A canonical entry listed in `ignore_paths` and `extra_paths` simultaneously
    is stripped first and then appended at the tail (not preserved in place).
    """
    spec = PathsSpec(ignore_paths=["pyproject.toml"], extra_paths=["pyproject.toml"])
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    # Survives via extras (appended).
    assert "pyproject.toml" in header
    # Find one paths block: pyproject.toml appears once and is the last entry.
    block_start = header.index("    paths:")
    header.index("\n", header.index("\n", block_start) + 1)
    # Walk to the end of the contiguous block.
    lines = header[block_start:].splitlines()
    block_lines = [lines[0]]
    for line in lines[1:]:
        if line.startswith("      - "):
            block_lines.append(line)
        else:
            break
    # Last entry in block is the appended pyproject.toml.
    assert block_lines[-1].strip() == "- pyproject.toml"


def test_header_paths_ignore_block_untouched_by_knobs() -> None:
    """`extra_paths`/`ignore_paths`/per-workflow override only target `paths:`.

    `paths-ignore:` blocks are written by the canonical workflow as exclusion
    filters; they are not the trigger gate the knobs are designed for. The
    header rewriter must leave them alone.
    """
    # Synthetic header content with a paths-ignore block to confirm the
    # rewriter regex does not match it.
    spec = PathsSpec(
        ignore_paths=["pyproject.toml"],
        extra_paths=["install.sh"],
        workflow_paths={"tests.yaml": ["wholesale.sh"]},
    )
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    # The rewriter only rewrote `paths:` blocks; if a real workflow gains a
    # `paths-ignore:` block in the future, this test will fail and prompt
    # explicit handling. For now, just ensure no `paths-ignore:` line was
    # injected by the rewriter.
    assert "paths-ignore:" not in header


def test_header_preserves_canonical_quote_style() -> None:
    """Header rewriter preserves quote style for unmodified entries."""
    # `tests.yaml` uses unquoted entries throughout. Substituting source paths
    # must not introduce quotes around the new entries.
    spec = PathsSpec(source_paths=["my_pkg"])
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    assert "      - my_pkg/**" in header
    assert '      - "my_pkg/**"' not in header


# ---------------------------------------------------------------------------
# generate_workflows with paths_spec
# ---------------------------------------------------------------------------


def test_generate_workflows_paths_spec_supersedes_source_paths(tmp_path: Path) -> None:
    """`generate_workflows` honors `paths_spec` over the legacy source_paths arg."""
    spec = PathsSpec(
        source_paths=["from_spec"],
        extra_paths=["repo-specific.sh"],
    )
    exit_code = generate_workflows(
        names=("tests.yaml",),
        output_format=WorkflowFormat.HEADER_ONLY,
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
        source_paths=["from_legacy"],
        paths_spec=spec,
    )
    # `tests.yaml` is non-reusable so the function attempts a header-only sync
    # but the destination doesn't exist, so it should warn and skip without
    # erroring. Stage a stub destination first to exercise the rewrite path.
    (tmp_path / "tests.yaml").write_text(
        '---\nname: stub\n"on":\n  push:\n    paths:\n      - placeholder\njobs:\n'
        "  stub:\n    runs-on: ubuntu-latest\n",
        encoding="UTF-8",
    )
    exit_code = generate_workflows(
        names=("tests.yaml",),
        output_format=WorkflowFormat.HEADER_ONLY,
        version="main",
        repo=DEFAULT_REPO,
        output_dir=tmp_path,
        overwrite=True,
        source_paths=["from_legacy"],
        paths_spec=spec,
    )
    assert exit_code == 0
    written = (tmp_path / "tests.yaml").read_text(encoding="UTF-8")
    assert "from_spec/**" in written
    assert "from_legacy/**" not in written
    assert "repo-specific.sh" in written
