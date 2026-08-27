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

import ast
from pathlib import Path

import pytest
import yaml

from repomatic.config import Config, WorkflowConfig
from repomatic.github import workflow_sync as ws
from repomatic.github.actions import AnnotationLevel
from repomatic.github.workflow_sync import (
    LintResult,
    PathsSpec,
    WorkflowTriggerInfo,
    _adapt_trigger_paths,
    _needs_yaml_quote,
    _split_yaml_quote,
    _substitute_source_paths,
    canonical_caller_permissions,
    check_has_workflow_dispatch,
    check_secrets_passed,
    check_triggers_match,
    check_version_pinned,
    extract_extra_jobs,
    extract_trigger_info,
    extras_define_jobs,
    generate_thin_caller,
    generate_workflow_header,
    identify_canonical_workflow,
    render_thin_caller_for_target,
    run_workflow_lint,
    workflow_triggers,
)
from repomatic.init_project import get_data_content
from repomatic.lint_repo import check_workflow_permissions
from repomatic.pyproject import derive_source_paths, resolve_source_paths
from repomatic.registry import (
    ALL_WORKFLOW_FILES,
    DEFAULT_REPO,
    NON_REUSABLE_WORKFLOWS,
    RELEASE_ENGINE_WORKFLOWS,
    REUSABLE_WORKFLOWS,
    UPSTREAM_SOURCE_GLOB,
    UPSTREAM_SOURCE_PREFIX,
    WORKFLOW_SOURCES,
)
from repomatic.tooling.tool_runner import run_tool
from tests.conftest import (
    PROJECT_ROOT,
    WORKFLOWS_WITH_CONCURRENCY_BLOCK,
    WORKFLOWS_WITHOUT_CONCURRENCY_BLOCK,
    skip_unless_tool_runs,
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


@pytest.mark.parametrize("filename", sorted(NON_REUSABLE_WORKFLOWS))
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


def test_unsubscribe_has_secrets() -> None:
    """Verify unsubscribe.yaml defines secrets."""
    info = extract_trigger_info("unsubscribe.yaml")
    assert "REPOMATIC_NOTIFICATIONS_PAT" in info.call_secrets


def test_unsubscribe_has_call_inputs() -> None:
    """Verify unsubscribe.yaml exposes its dispatch inputs to callers."""
    info = extract_trigger_info("unsubscribe.yaml")
    assert set(info.call_inputs) == {"batch-size", "dry-run", "months"}


# The complete workflow_call input surface every thin-caller-deployed reusable
# workflow forwards. Only unsubscribe.yaml (config-gated, off by default) carries
# any; the rest forward nothing.
EXPECTED_THIN_CALLER_CALL_INPUTS: dict[str, set[str]] = {
    "unsubscribe.yaml": {"batch-size", "dry-run", "months"},
}


def test_thin_caller_workflow_call_inputs_stay_minimal() -> None:
    """No reusable workflow may grow its forwarded workflow_call inputs unreviewed.

    Every input a thin caller forwards widens the blast radius of the cooldown
    step-back in {func}`repomatic.init_project.resolve_default_pin`: that pin can
    reference a reusable workflow one release older than the caller was generated
    from, and GitHub rejects a forwarded input the older workflow does not yet
    declare. Locking the surface keeps a new input a deliberate act, weighed
    against the step-back, not an accident. To relax it, add the input to
    EXPECTED_THIN_CALLER_CALL_INPUTS with that trade-off in mind.
    """
    forwarded = {
        filename: set(
            extract_trigger_info(WORKFLOW_SOURCES.get(filename, filename)).call_inputs
        )
        for filename in REUSABLE_WORKFLOWS
    }
    non_empty = {name: inputs for name, inputs in forwarded.items() if inputs}
    assert non_empty == EXPECTED_THIN_CALLER_CALL_INPUTS


def test_unsubscribe_caller_forwards_inputs() -> None:
    """Verify the generated caller forwards each input with the right expression.

    Boolean inputs are coerced with ``== true`` so non-dispatch events
    (schedule), where the caller's ``inputs`` context is null, pass an explicit
    ``false`` instead of relying on GitHub's undocumented handling of a null
    value for a boolean-typed input.
    """
    content = generate_thin_caller("unsubscribe.yaml")
    data = yaml.safe_load(content)
    assert data["jobs"]["unsubscribe"]["with"] == {
        "months": "${{ inputs.months }}",
        "batch-size": "${{ inputs.batch-size }}",
        "dry-run": "${{ inputs.dry-run == true }}",
    }


# release.yaml is excluded: its multi-lane caller is synthesized by
# _generate_release_caller, not the single-job thin delegation checked here.
@pytest.mark.parametrize(
    "filename", [f for f in REUSABLE_WORKFLOWS if f != "release.yaml"]
)
def test_caller_with_block_matches_call_inputs(filename: str) -> None:
    """Caller emits a ``with:`` forwarding block only for declared call inputs."""
    content = generate_thin_caller(filename)
    data = yaml.safe_load(content)
    job = data["jobs"][filename.removesuffix(".yaml")]
    info = extract_trigger_info(WORKFLOW_SOURCES.get(filename, filename))
    if info.call_inputs:
        assert set(job["with"]) == set(info.call_inputs)
    else:
        assert "with" not in job


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_no_python_literals_in_yaml(filename: str) -> None:
    """Verify generated YAML contains no Python dict/list literals.

    Regression test for a bug where ``_render_trigger_value`` fell through to
    ``str()`` on nested dicts, producing ``{'key': 'value'}`` instead of
    block-style YAML.

    Kept alongside the two linter passes below, which do not subsume it.
    actionlint rejects a leaked literal only where the workflow schema
    constrains the value it landed in (a flow mapping under `schedule:`, which
    takes a list); the same leak inside a free-form value, a `paths:` entry or
    an input default, is well-formed YAML that both linters accept. This scans
    the rendered text wherever it appears, and names the defect directly.
    """
    content = generate_thin_caller(filename)
    assert "{'" not in content, f"{filename}: Python dict literal found in output"
    assert "'}" not in content, f"{filename}: Python dict literal found in output"


PINNED_CALLER_SHA = "0" * 40
"""Placeholder commit SHA for the pinned `uses:` rendering.

actionlint checks the *shape* of a `uses:` reference without resolving it, so a
synthetic SHA exercises that branch with no network lookup and no tie to a
commit that has to keep existing.
"""


def _materialize_thin_callers(root: Path) -> list[str]:
    """Write every generated thin caller under *root* as a real workflow tree.

    Both `uses:` renderings are covered: the bare `@version` tag ref, and the
    `@sha # version` form `repomatic init` writes into a downstream repo. The
    two differ in the trailing comment on the `uses:` line, so linting one
    leaves the other's rendering unchecked.

    Files land under a `.github/workflows/` path because actionlint keys part
    of its rule set on that location.

    :param root: Directory to build the workflow trees under.
    :return: Every written path, ready to hand to a linter.
    """
    written: list[str] = []
    for variant, commit_sha in (("tag-ref", None), ("sha-pinned", PINNED_CALLER_SHA)):
        workflows = root / variant / ".github" / "workflows"
        workflows.mkdir(parents=True)
        for filename in REUSABLE_WORKFLOWS:
            content = generate_thin_caller(
                filename, version="v9.9.9", commit_sha=commit_sha
            )
            target = workflows / filename
            target.write_text(content, encoding="UTF-8")
            written.append(str(target))
    return written


@pytest.mark.once
def test_generated_callers_pass_yamllint(tmp_path: Path) -> None:
    """Generated thin callers must clear the yamllint rules CI enforces.

    A thin caller is the one artifact this repository generates and never
    materializes into its own tree: `.github/workflows/` here holds the
    canonical workflows, so the `lint` job's yamllint step never sees a
    generated file. The first checkout that does is a downstream repo, a
    release later, which makes a regression here something a user reports
    rather than something CI catches. Running the pinned yamllint over the
    output moves that verdict back to the commit that introduced it.

    In practice that is the 120-column cap and little else: a native config
    replaces yamllint's defaults rather than layering over them, and the
    bundled `yamllint.yaml` sets `line-length` alone. It is still the rule
    worth guarding, being the one a renderer breaches by widening a single
    interpolated value, and the one `tests/test_prepare_release.py`
    re-implements in Python to cover the freeze path. Any rule the bundled
    config grows later is covered here for free.

    Marked `once`: it resolves yamllint through uvx, so one runner suffices.
    """
    skip_unless_tool_runs("yamllint")
    assert run_tool("yamllint", extra_args=_materialize_thin_callers(tmp_path)) == 0, (
        "yamllint rejects generated thin-caller output; its diagnostics are in "
        "the captured stdout above. Fix the renderers in "
        "repomatic/github/workflow_sync.py rather than any workflow in this "
        "repository: this output only ever lands in a downstream checkout."
    )


@pytest.mark.once
def test_generated_callers_pass_actionlint(tmp_path: Path) -> None:
    """Generated thin callers must be workflows GitHub will actually accept.

    Companion to the yamllint pass, and the stronger half of the pair: it
    reads the workflow schema, so it sees what neither a YAML parser nor a
    column cap can. A `uses:` reference stripped of its `@ref`, a `${{ }}`
    expression naming a context property that does not exist, a trigger value
    whose type is wrong for the key it sits under: each parses cleanly, and
    each fails at startup on the runner. For a generated caller that means
    failing in someone else's repository.

    Marked `once`: it downloads the pinned actionlint binary, so one runner
    suffices.
    """
    skip_unless_tool_runs("actionlint")
    assert (
        run_tool("actionlint", extra_args=_materialize_thin_callers(tmp_path)) == 0
    ), (
        "actionlint rejects generated thin-caller output; its diagnostics are "
        "in the captured stdout above. Fix the renderers in "
        "repomatic/github/workflow_sync.py rather than any workflow in this "
        "repository: this output only ever lands in a downstream checkout."
    )


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_call_inputs_are_dispatch_passthroughs(filename: str) -> None:
    """Verify ``workflow_call`` inputs only mirror ``workflow_dispatch`` ones.

    All persistent configuration lives in ``[tool.repomatic]`` in
    ``pyproject.toml``: workflows read config via the ``repomatic`` CLI instead
    of accepting inputs. The only ``workflow_call`` inputs allowed are
    passthroughs of the workflow's own ``workflow_dispatch`` inputs, so
    generated thin callers can forward a manual dispatch to the reusable
    workflow (see ``_generate_thin_caller``). Type and default must match the
    dispatch declaration, so both entry points behave identically.
    """
    info = extract_trigger_info(WORKFLOW_SOURCES.get(filename, filename))
    dispatch_config = info.non_call_triggers.get("workflow_dispatch") or {}
    dispatch_inputs = dispatch_config.get("inputs") or {}
    for input_name, call_input in info.call_inputs.items():
        assert input_name in dispatch_inputs, (
            f"{filename}: workflow_call input {input_name!r} has no"
            " workflow_dispatch counterpart"
        )
        for prop in ("type", "default"):
            assert call_input.get(prop) == dispatch_inputs[input_name].get(prop), (
                f"{filename}: workflow_call input {input_name!r} {prop} differs"
                " from its workflow_dispatch declaration"
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
    triggers = workflow_triggers(data)
    canonical = extract_trigger_info(filename)
    canonical_has_dispatch = "workflow_dispatch" in canonical.non_call_triggers
    assert ("workflow_dispatch" in triggers) is canonical_has_dispatch


def test_release_thin_caller_synthesizes_triggers() -> None:
    """The release.yaml caller synthesizes the standard push + dispatch triggers.

    `_generate_release_caller` synthesizes the standard release triggers rather
    than mirroring the canonical release.yaml's own (so a dogfooding-only trigger
    added upstream never leaks into downstream callers). The weekly full-fleet
    `schedule` is the one canonical trigger carried over, verbatim.
    """
    content = generate_thin_caller("release.yaml")
    data = yaml.safe_load(content)
    triggers = workflow_triggers(data)
    assert "workflow_dispatch" in triggers
    assert triggers["push"] == {"branches": ["main"]}
    canonical_schedule = extract_trigger_info("release.yaml").non_call_triggers[
        "schedule"
    ]
    assert triggers["schedule"] == canonical_schedule
    # The engine lane is call-only.
    assert extract_trigger_info("_release-engine.yaml").non_call_triggers == {}


def test_release_caller_emits_concurrency() -> None:
    """The release.yaml caller carries the entry workflow's concurrency group.

    Unlike a simple thin caller, the multi-lane release caller must declare
    concurrency on its push-triggered entry: the concurrency-bearing engine lane
    runs behind ``needs: build``, so a block there joins its group too late to
    cancel superseded runs. The group protects release commits via github.sha.
    """
    content = generate_thin_caller("release.yaml", version="v9.9.9")
    data = yaml.safe_load(content)
    assert "concurrency" in data
    group = data["concurrency"]["group"]
    assert "github.sha" in group
    assert "[changelog] Release" in group
    assert "[changelog] Post-release" in group
    assert data["concurrency"]["cancel-in-progress"] is True


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
    """Verify the release.yaml caller emits the publish-pypi job gated on the build lane."""
    content = generate_thin_caller("release.yaml", version="v9.9.9")
    assert "  publish-pypi:" in content
    # publish-pypi depends only on the build lane so the wheel ships to PyPI right
    # after it is built, not after the whole engine (binaries, scan) completes.
    assert "needs: build" in content
    # The gate is decoupled from the overall run result: always() + package_built
    # let a healthy wheel publish even when an unrelated job (like binary tests)
    # fails the run. Both signals come from the build lane.
    assert "always()" in content
    assert "needs.build.outputs.package_built == 'true'" in content
    assert "needs.build.outputs.release_commits_matrix" in content
    assert "id-token: write" in content
    assert f"{DEFAULT_REPO}/.github/actions/publish-pypi@v9.9.9" in content
    assert (
        "artifact-name: ${{ github.event.repository.name }}-${{ matrix.short_sha }}"
        in content
    )


def test_release_thin_caller_emits_build_and_engine_lanes() -> None:
    """Verify the release.yaml caller calls both reusable lanes with pinned refs.

    The build lane (`_release-build.yaml`) feeds publish-pypi; the engine lane
    (`_release-engine.yaml`) runs binaries and finalization. Both are pinned to
    the requested version and the engine lane is gated on the build lane so it
    can download the run-scoped wheel.
    """
    content = generate_thin_caller("release.yaml", version="v9.9.9")
    for lane in RELEASE_ENGINE_WORKFLOWS:
        assert f"{DEFAULT_REPO}/.github/workflows/{lane}@v9.9.9" in content, (
            f"generated release.yaml must reference the {lane} engine lane"
        )
    # The engine lane waits for the build lane (run-scoped wheel handoff).
    data = yaml.safe_load(content)
    assert data["jobs"]["release"]["needs"] == "build"


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
    third-party action SHA pins (which `sync-action-pins` would not see, since
    the generator's source is `.py`, not `.yaml`).
    """
    content = generate_thin_caller("release.yaml")
    assert "actions/checkout" not in content


def test_release_thin_caller_publish_pypi_sourced_from_release_yaml() -> None:
    """The publish-pypi job body must be sourced from the bundled release.yaml.

    Deriving it from a `.yaml` file (rather than building it in Python) keeps
    any future third-party SHA pin in the job body visible to `sync-action-pins`.
    The canonical entry carries the job with a local `./` action ref that the
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
    checkout drop and the action-ref pin, so downstream callers run on whichever
    image repomatic itself uses. The expectation is read from that source rather
    than written here: pinning the literal made this fail as a fleet migration
    rather than as the drift it exists to catch.
    """
    source = yaml.safe_load(get_data_content("release.yaml"))
    canonical = source["jobs"]["publish-pypi"]["runs-on"]
    assert not canonical.endswith("-latest"), (
        f"release.yaml's publish-pypi runs on {canonical!r}, a floating alias "
        "that GitHub repoints without a commit here."
    )

    content = generate_thin_caller("release.yaml", version="v9.9.9")
    assert f"runs-on: {canonical}" in content
    assert "ubuntu-latest" not in content


def _patch_release_yaml(monkeypatch, body: str) -> None:
    """Override only the bundled release.yaml, delegating other names to disk.

    `get_data_content` also fetches `_release-engine.yaml` for trigger
    extraction; a blanket override would feed the test body to that call and
    break `extract_trigger_info`.
    """
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
        "  build:\n"
        "    uses: ./.github/workflows/_release-build.yaml\n"
        "  publish-pypi:\n"
        "    permissions:\n"
        "      id-token: write\n"
        "    runs-on: ubuntu-slim\n"
        "    steps:\n"
        "      - uses: actions/checkout@abc123\n"
        "      - uses: ./.github/actions/publish-pypi\n"
        "        with:\n"
        "          artifact-name: x\n"
        "  release:\n"
        "    uses: ./.github/workflows/_release-engine.yaml\n",
    )

    content = generate_thin_caller("release.yaml", version="v9.9.9")
    assert f"{DEFAULT_REPO}/.github/actions/publish-pypi@v9.9.9" in content
    assert "./.github/actions/publish-pypi" not in content
    assert "actions/checkout" not in content
    # Both reusable lanes are pinned to the requested version.
    assert f"{DEFAULT_REPO}/.github/workflows/_release-build.yaml@v9.9.9" in content
    assert f"{DEFAULT_REPO}/.github/workflows/_release-engine.yaml@v9.9.9" in content


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
        "  build:\n"
        "    uses: ./.github/workflows/_release-build.yaml\n"
        "  publish-pypi:\n"
        "    permissions:\n"
        "      id-token: write\n"
        "    steps:\n"
        "      - run: echo no-action-ref-here\n"
        "  release:\n"
        "    uses: ./.github/workflows/_release-engine.yaml\n",
    )

    with pytest.raises(RuntimeError, match="publish-pypi"):
        generate_thin_caller("release.yaml", version="v9.9.9")


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


def test_extract_extra_jobs_odd_indent_body_stays_managed() -> None:
    """A hand-edited 3-space body line does not truncate the managed job.

    The body walk used to require exactly 4+ spaces, so a 3-space line ended
    it early and the managed job's tail leaked into the extras, which the
    next sync duplicated below the regenerated lanes.
    """
    content = (
        f"---\nname: Release\njobs:\n\n  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/release.yaml@v6.0.0\n"
        "   # Hand-edited three-space comment line.\n"
        "    secrets:\n"
        "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
        "\n"
        "  mine:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hello\n"
    )
    extra = extract_extra_jobs(content)
    assert "REPOMATIC_PAT" not in extra
    assert "three-space comment" not in extra
    assert "mine:" in extra


def test_extras_define_jobs_ignores_comment_only_tail() -> None:
    """A trailing comment after the managed lanes is not a downstream job.

    `bool(extra)` used to gate the explicit-permissions contract, so a
    repository keeping a plain trailing note below its thin caller was
    silently flipped onto the `permissions: {}` shape it never asked for.
    """
    assert not extras_define_jobs("")
    assert not extras_define_jobs("  # Trailing note about the job above.\n")
    assert not extras_define_jobs("\n  # One.\n\n  # Two.\n")
    assert extras_define_jobs("  mine:\n    runs-on: ubuntu-latest\n")
    assert extras_define_jobs("  # Heading.\n  mine:\n    runs-on: ubuntu-latest\n")


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


def sync_caller(
    filename: str,
    target: Path,
    version: str = "v1.2.3",
    commit_sha: str | None = None,
    paths_spec: PathsSpec | None = None,
) -> None:
    """Render *filename* onto *target*, the way `repomatic init` syncs one file.

    Wraps the single render seam so a test never re-implements the read-render-
    write dance that `_init_workflows` performs, which is how the two used to
    drift apart.
    """
    content, _existing = render_thin_caller_for_target(
        filename,
        target,
        repo=DEFAULT_REPO,
        version=version,
        commit_sha=commit_sha,
        paths_spec=paths_spec,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="UTF-8")


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
    target = tmp_path / "release.yaml"
    sync_caller("release.yaml", target, version="v6.0.0")
    target.write_text(
        target.read_text(encoding="UTF-8") + extra_job,
        encoding="UTF-8",
    )

    # Re-sync with a new version. The extra job must survive.
    sync_caller("release.yaml", target, version="v7.0.0")
    result = target.read_text(encoding="UTF-8")
    # Managed job was updated.
    assert "v7.0.0" in result
    assert "v6.0.0" not in result
    # Extra job was preserved.
    assert "chocolatey:" in result
    assert "# Custom packaging job." in result


@pytest.mark.parametrize(
    ("body", "is_issue", "needle", "level"),
    [
        pytest.param(
            '---\n"on":\n  workflow_dispatch:\n  push:\n',
            False,
            "",
            AnnotationLevel.WARNING,
            id="present",
        ),
        pytest.param(
            '---\n"on":\n  push:\n',
            True,
            "missing",
            AnnotationLevel.WARNING,
            id="absent",
        ),
        # Unparsable YAML is an error, not a warning: nothing downstream can
        # read the file either.
        pytest.param(
            "{{invalid",
            True,
            "",
            AnnotationLevel.ERROR,
            id="invalid-yaml",
        ),
    ],
)
def test_check_has_workflow_dispatch(
    tmp_path: Path, body: str, is_issue: bool, needle: str, level: AnnotationLevel
) -> None:
    """The `workflow_dispatch` check reports presence, absence and unreadability."""
    wf = tmp_path / "candidate.yaml"
    wf.write_text(body, encoding="UTF-8")
    result = check_has_workflow_dispatch(wf)
    assert result.is_issue is is_issue
    assert needle in result.message
    assert result.level == level


@pytest.mark.parametrize(
    ("ref", "is_issue", "needle"),
    [
        pytest.param("v5.8.0", False, "", id="version-tag"),
        pytest.param("main", True, "@main", id="floating-branch"),
    ],
)
def test_check_version_pinned(
    tmp_path: Path, ref: str, is_issue: bool, needle: str
) -> None:
    """A caller must pin the upstream workflow to a tag, never to a branch."""
    wf = tmp_path / "lint.yaml"
    wf.write_text(
        f"    uses: {DEFAULT_REPO}/.github/workflows/lint.yaml@{ref}\n",
        encoding="UTF-8",
    )
    result = check_version_pinned(wf)
    assert result.is_issue is is_issue
    assert needle in result.message


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


def test_caller_with_synthetic_triggers_for_call_only_canonical(
    tmp_path: Path,
) -> None:
    """Caller with synthesized triggers is clean when canonical has no non-call triggers.

    The downstream release.yaml synthesizes push + workflow_dispatch because
    _release-engine.yaml is call-only. check_triggers_match must not flag
    those synthesized triggers as "extra" when the canonical defines none.
    """
    content = generate_thin_caller("release.yaml", version="v9.9.9")
    wf = tmp_path / "release.yaml"
    wf.write_text(content, encoding="UTF-8")
    canonical = identify_canonical_workflow(wf)
    assert canonical == "_release-engine.yaml"
    result = check_triggers_match(wf, canonical)
    assert result.is_issue is False


ALL_RELEASE_SECRETS = (
    "    secrets:\n"
    "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
    "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n"
)
"""Every secret `_release-engine.yaml` declares, passed explicitly."""


@pytest.mark.parametrize(
    ("caller", "canonical", "secrets_block", "is_issue", "needle"),
    [
        pytest.param(
            "release",
            "_release-engine.yaml",
            ALL_RELEASE_SECRETS,
            False,
            "",
            id="all-passed-explicitly",
        ),
        # `secrets: inherit` predates the explicit form and still satisfies it.
        pytest.param(
            "release",
            "_release-engine.yaml",
            "    secrets: inherit\n",
            False,
            "",
            id="inherit",
        ),
        pytest.param(
            "release",
            "_release-engine.yaml",
            "",
            True,
            "secrets",
            id="none-passed",
        ),
        pytest.param(
            "release",
            "_release-engine.yaml",
            "    secrets:\n"
            "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n",
            True,
            "REPOMATIC_PAT",
            id="partially-passed",
        ),
        pytest.param(
            "lint",
            "lint.yaml",
            "    secrets:\n"
            "      CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n"
            "      REPOMATIC_NOTIFICATIONS_PAT:"
            " ${{ secrets.REPOMATIC_NOTIFICATIONS_PAT }}\n"
            "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
            "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n",
            False,
            "",
            id="lint-all-passed",
        ),
    ],
)
def test_check_secrets_passed(
    tmp_path: Path,
    caller: str,
    canonical: str,
    secrets_block: str,
    is_issue: bool,
    needle: str,
) -> None:
    """A caller must forward every secret its canonical workflow declares."""
    wf = tmp_path / f"{caller}.yaml"
    wf.write_text(
        f"---\njobs:\n  {caller}:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/{caller}.yaml@v5.8.0\n"
        f"{secrets_block}",
        encoding="UTF-8",
    )
    result = check_secrets_passed(wf, canonical)
    assert result.is_issue is is_issue
    assert needle in result.message


CLEAN_WORKFLOW = (
    '---\n"on":\n  workflow_dispatch:\n  push:\njobs:\n'
    "  build:\n    runs-on: ubuntu-latest\n"
)
"""A workflow the linter finds nothing to say about."""

DISPATCHLESS_WORKFLOW = '---\n"on":\n  push:\n'
"""A workflow missing `workflow_dispatch`, the linter's mildest complaint."""


@pytest.mark.parametrize(
    ("body", "fatal", "exit_code"),
    [
        pytest.param(CLEAN_WORKFLOW, False, 0, id="clean"),
        pytest.param(None, False, 0, id="empty-directory"),
        # The severity knob only moves the exit code, never the diagnosis.
        pytest.param(DISPATCHLESS_WORKFLOW, False, 0, id="issue-warning-mode"),
        pytest.param(DISPATCHLESS_WORKFLOW, True, 1, id="issue-fatal-mode"),
    ],
)
def test_run_workflow_lint(
    tmp_path: Path, body: str | None, fatal: bool, exit_code: int
) -> None:
    """Lint exit codes across a clean tree, an empty one, and both severities."""
    if body is not None:
        (tmp_path / "candidate.yaml").write_text(body, encoding="UTF-8")
    assert run_workflow_lint(tmp_path, fatal=fatal) == exit_code


def test_run_workflow_lint_missing_directory(tmp_path: Path) -> None:
    """A directory that does not exist is an error, not an empty tree."""
    assert run_workflow_lint(tmp_path / "nonexistent") == 1


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


def test_release_thin_caller_lints_clean(tmp_path: Path) -> None:
    """The generated downstream release.yaml lints clean end-to-end.

    Regression test: the synthesized push + workflow_dispatch triggers must not
    be flagged as "extra" by check_triggers_match when the canonical
    _release-engine.yaml defines no non-call triggers.
    """
    content = generate_thin_caller("release.yaml", version="v5.8.0")
    (tmp_path / "release.yaml").write_text(content, encoding="UTF-8")
    exit_code = run_workflow_lint(tmp_path, fatal=True)
    assert exit_code == 0


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_every_reusable_workflow_renders(tmp_path: Path, filename: str) -> None:
    """Each reusable workflow renders into a caller carrying the version ref."""
    target = tmp_path / filename
    sync_caller(filename, target, version="v5.8.0")
    assert "v5.8.0" in target.read_text(encoding="UTF-8")


def test_sync_overwrites(tmp_path: Path) -> None:
    """A file that is not a caller at all is replaced wholesale.

    Nothing in it parses as a managed lane, so there are no extras to carry
    over and the render starts from the canonical workflow.
    """
    target = tmp_path / "lint.yaml"
    target.write_text("old content", encoding="UTF-8")
    sync_caller("lint.yaml", target)
    assert "old content" not in target.read_text(encoding="UTF-8")


def test_non_reusable_workflow_refuses_to_render(tmp_path: Path) -> None:
    """A workflow with no `workflow_call` trigger cannot become a caller.

    `tests.yaml` is copied downstream header-first by `_init_workflows`, never
    rendered as a caller, so asking for one is a programming error rather than
    a file to write.
    """
    with pytest.raises(ValueError, match="workflow_call"):
        sync_caller("tests.yaml", tmp_path / "tests.yaml")


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

# `extract_trigger_info` reads the *bundled* copy of a workflow, so both rosters
# are narrowed to what ships: `self-maintenance.yaml` and `_release-build.yaml`
# live in `.github/workflows/` but have no counterpart under `repomatic/data/`,
# and asking for one raises FileNotFoundError rather than reporting an absent
# concurrency block.
BUNDLED_WORKFLOWS = frozenset(
    p.name for p in (PROJECT_ROOT / "repomatic" / "data").glob("*.yaml")
)

BUNDLED_WITH_CONCURRENCY = tuple(
    name for name in WORKFLOWS_WITH_CONCURRENCY_BLOCK if name in BUNDLED_WORKFLOWS
)
"""Bundled workflows that define a concurrency block."""

BUNDLED_WITHOUT_CONCURRENCY = tuple(
    name for name in WORKFLOWS_WITHOUT_CONCURRENCY_BLOCK if name in BUNDLED_WORKFLOWS
)
"""Bundled workflows that do not.

`_release-engine.yaml` is here by design: it delegates concurrency to the
push-triggered `release.yaml` entry that calls it (see
{func}`_generate_release_caller`).
"""


@pytest.mark.parametrize("filename", BUNDLED_WITH_CONCURRENCY)
def test_concurrency_present(filename: str) -> None:
    """Verify concurrency is extracted for workflows that define it."""
    info = extract_trigger_info(filename)
    assert info.concurrency is not None
    assert info.raw_concurrency is not None
    assert "concurrency:" in info.raw_concurrency


@pytest.mark.parametrize("filename", BUNDLED_WITHOUT_CONCURRENCY)
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
    # The release entry has explanatory comments in its concurrency block.
    assert "#" in info.raw_concurrency


# ---------------------------------------------------------------------------
# Thin caller omits concurrency
# ---------------------------------------------------------------------------


# release.yaml is excluded: its multi-lane caller carries concurrency because
# the engine lane it calls joins its group too late to cancel queued runs. See
# test_release_caller_emits_concurrency.
@pytest.mark.parametrize(
    "filename", [f for f in REUSABLE_WORKFLOWS if f != "release.yaml"]
)
def test_thin_caller_omits_concurrency(filename: str) -> None:
    """Verify simple thin callers never include concurrency.

    A simple thin caller's single job joins the reusable workflow's own
    concurrency group immediately when called via ``workflow_call``, so
    duplicating the block in the caller is unnecessary.
    """
    content = generate_thin_caller(filename)
    assert "concurrency:" not in content


# ---------------------------------------------------------------------------
# Thin caller omits upstream-only paths but keeps universal entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", REUSABLE_WORKFLOWS)
def test_thin_caller_drops_upstream_source_paths(filename: str) -> None:
    """Thin callers drop `repomatic/`-prefixed paths even without source_paths.

    Universal entries (`pyproject.toml`, workflow self-refs) are preserved so
    trigger semantics carry over from the canonical workflow.
    """
    content = generate_thin_caller(filename)
    data = yaml.safe_load(content)
    triggers = workflow_triggers(data)
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
# Source path substitution tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "source_paths", "expected"),
    [
        pytest.param(
            [UPSTREAM_SOURCE_GLOB, "tests/**", "pyproject.toml"],
            ["extra_platforms"],
            ["extra_platforms/**", "tests/**", "pyproject.toml"],
            id="glob-replaced-in-place",
        ),
        pytest.param(
            [UPSTREAM_SOURCE_GLOB, "pyproject.toml"],
            ["pkg_a", "pkg_b"],
            ["pkg_a/**", "pkg_b/**", "pyproject.toml"],
            id="glob-fans-out-to-several",
        ),
        # An upstream path that is not the source glob has no downstream
        # counterpart to become, so it is dropped rather than translated.
        pytest.param(
            [
                UPSTREAM_SOURCE_GLOB,
                f"{UPSTREAM_SOURCE_PREFIX}data/labels.toml",
                "config.json",
            ],
            ["my_pkg"],
            ["my_pkg/**", "config.json"],
            id="upstream-specific-dropped",
        ),
        pytest.param(
            ["tests/**", "pyproject.toml", "uv.lock", "changelog.md"],
            ["my_pkg"],
            ["tests/**", "pyproject.toml", "uv.lock", "changelog.md"],
            id="universal-paths-untouched",
        ),
        pytest.param(
            [UPSTREAM_SOURCE_GLOB, "pyproject.toml"],
            [],
            ["pyproject.toml"],
            id="no-source-paths-drops-the-glob",
        ),
    ],
)
def test_substitute_source_paths(
    paths: list[str], source_paths: list[str], expected: list[str]
) -> None:
    """Upstream source globs are retargeted; everything else is kept or dropped."""
    assert _substitute_source_paths(paths, source_paths) == expected


@pytest.mark.parametrize(
    ("config", "filename", "spec", "expected"),
    [
        pytest.param(
            {"branches": ["main"], "paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml"]},
            "tests.yaml",
            PathsSpec(source_paths=["extra_platforms"]),
            {"branches": ["main"], "paths": ["extra_platforms/**", "pyproject.toml"]},
            id="source-paths-substituted",
        ),
        pytest.param(
            {
                "branches": ["main"],
                "paths": [
                    UPSTREAM_SOURCE_GLOB,
                    "pyproject.toml",
                    "repomatic/data/labels.toml",
                ],
            },
            "tests.yaml",
            PathsSpec(),
            {"branches": ["main"], "paths": ["pyproject.toml"]},
            id="no-source-paths-keeps-universal",
        ),
        # Emptying the list drops the key: an empty `paths:` matches nothing,
        # which would silently disable the trigger.
        pytest.param(
            {
                "branches": ["main"],
                "paths": [UPSTREAM_SOURCE_GLOB, "repomatic/data/labels.toml"],
            },
            "tests.yaml",
            PathsSpec(),
            {"branches": ["main"]},
            id="all-upstream-drops-the-key",
        ),
        pytest.param(
            {"branches": ["main"]},
            "tests.yaml",
            PathsSpec(source_paths=["extra_platforms"]),
            {"branches": ["main"]},
            id="no-paths-key-passes-through",
        ),
        pytest.param(
            {"paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml"]},
            "tests.yaml",
            PathsSpec(
                source_paths=["my_pkg"],
                # The already-present entry is deduped rather than repeated.
                extra_paths=["install.sh", "pyproject.toml"],
            ),
            {"paths": ["my_pkg/**", "pyproject.toml", "install.sh"]},
            id="extra-paths-appended",
        ),
        pytest.param(
            {"paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml", "uv.lock"]},
            "tests.yaml",
            PathsSpec(source_paths=["my_pkg"], ignore_paths=["uv.lock"]),
            {"paths": ["my_pkg/**", "pyproject.toml"]},
            id="ignore-paths-stripped",
        ),
        pytest.param(
            {"paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml", "uv.lock"]},
            "tests.yaml",
            PathsSpec(
                source_paths=["my_pkg"],
                extra_paths=["never-applied.sh"],
                ignore_paths=["pyproject.toml"],
                workflow_paths={"tests.yaml": ["install.sh", "packages.toml"]},
            ),
            {"paths": ["install.sh", "packages.toml"]},
            id="override-wins-over-every-other-knob",
        ),
        pytest.param(
            {"paths": [UPSTREAM_SOURCE_GLOB, "pyproject.toml"]},
            "lint.yaml",
            PathsSpec(
                source_paths=["my_pkg"],
                workflow_paths={"tests.yaml": ["install.sh"]},
            ),
            {"paths": ["my_pkg/**", "pyproject.toml"]},
            id="override-scoped-to-its-own-file",
        ),
    ],
)
def test_adapt_trigger_paths(
    config: dict, filename: str, spec: PathsSpec, expected: dict
) -> None:
    """Every `PathsSpec` knob, applied to one trigger's config block."""
    assert _adapt_trigger_paths(config, filename, spec) == expected


# ---------------------------------------------------------------------------
# Thin caller with source paths
# ---------------------------------------------------------------------------


def test_thin_caller_release_with_source_paths() -> None:
    """Verify release.yaml thin caller has no path filters.

    release.yaml only has ``push`` (no ``paths:``) and ``workflow_call``,
    so ``source_paths`` has no effect.
    """
    content = generate_thin_caller(
        "release.yaml", paths_spec=PathsSpec(source_paths=["extra_platforms"])
    )
    data = yaml.safe_load(content)
    triggers = workflow_triggers(data)
    push_config = triggers.get("push", {})
    # push trigger has branches but no paths.
    assert "paths" not in push_config
    assert "pull_request" not in triggers


def test_thin_caller_changelog_with_source_paths() -> None:
    """Verify changelog.yaml thin caller keeps universal paths unchanged.

    changelog.yaml has no upstream source glob, so source_paths has no effect
    beyond keeping all paths intact.
    """
    content = generate_thin_caller(
        "changelog.yaml", paths_spec=PathsSpec(source_paths=["extra_platforms"])
    )
    data = yaml.safe_load(content)
    triggers = workflow_triggers(data)
    push_config = triggers.get("push", {})
    assert "paths" in push_config
    assert "changelog.md" in push_config["paths"]


def test_thin_caller_lint_no_paths_with_source_paths() -> None:
    """Verify workflows without paths don't gain paths from source_paths."""
    content = generate_thin_caller(
        "lint.yaml", paths_spec=PathsSpec(source_paths=["extra_platforms"])
    )
    data = yaml.safe_load(content)
    triggers = workflow_triggers(data)
    push_config = triggers.get("push", {})
    # lint.yaml has no paths filter in canonical, so none in thin caller.
    assert "paths" not in push_config


# ---------------------------------------------------------------------------
# Header generation with source paths
# ---------------------------------------------------------------------------


def test_header_with_source_paths_substitutes() -> None:
    """Verify header generation replaces upstream source paths."""
    header = generate_workflow_header(
        "tests.yaml", paths_spec=PathsSpec(source_paths=["my_pkg"])
    )
    assert "my_pkg/**" in header
    assert UPSTREAM_SOURCE_GLOB not in header


def test_header_without_source_paths_drops_upstream_glob() -> None:
    """Without source_paths, the upstream source glob is dropped from the header."""
    header = generate_workflow_header("tests.yaml")
    assert UPSTREAM_SOURCE_GLOB not in header
    # Universal entries survive.
    assert "pyproject.toml" in header


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
    header = generate_workflow_header("docs.yaml", paths_spec=spec)
    # The tests.yaml-scoped override does not leak into docs.yaml's header.
    assert "install.sh" not in header


@pytest.mark.parametrize(
    "entry",
    (
        "**/*.py",
        "*.py",
        "&anchor.py",
        "!tagged.py",
        "{flow}.py",
        "[seq].py",
        "yes",
        "1.0",
    ),
)
def test_header_override_quotes_entries_needing_it(entry: str) -> None:
    """Override entries that cannot stand as plain scalars are quoted.

    `tests.yaml` ships a `paths:` block whose entries are all plain, so there
    is no quote style to inherit. Emitting one of these bare produced a header
    that stopped parsing, which GitHub answers by ignoring the workflow.
    """
    spec = PathsSpec(workflow_paths={"tests.yaml": [entry, "pyproject.toml"]})
    header = generate_workflow_header("tests.yaml", paths_spec=spec)
    parsed = yaml.safe_load(header)
    # Both the push and pull_request blocks round-trip to the exact strings.
    for trigger in ("push", "pull_request"):
        assert parsed["on"][trigger]["paths"] == [entry, "pyproject.toml"]


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        # Indicators that open something other than a scalar.
        ("**/*.py", True),
        ("*.py", True),
        ("&anchor", True),
        ("!tag", True),
        ("{flow}", True),
        ("[seq]", True),
        # Plain but coerced away from str, which an indicator table would miss.
        ("yes", True),
        ("null", True),
        ("1.0", True),
        ("", True),
        # Ordinary path filters stay unquoted, so canonical blocks do not churn.
        ("pyproject.toml", False),
        ("uv.lock", False),
        ("content/**", False),
        ("docs/**", False),
        (".github/workflows/tests.yaml", False),
    ),
)
def test_needs_yaml_quote(value: str, expected: bool) -> None:
    """Only entries that would not round-trip as plain scalars are flagged."""
    assert _needs_yaml_quote(value) is expected


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
    pyproject_data = {"project": {"name": "papaya-press"}}
    result = resolve_source_paths(config, pyproject_data)
    assert result == ["papaya_press"]


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
# Thin caller with full paths_spec
# ---------------------------------------------------------------------------


def test_thin_caller_paths_spec_extra_paths_appends() -> None:
    """Thin caller picks up `extra_paths` in every paths-bearing trigger."""
    spec = PathsSpec(extra_paths=["install.sh"])
    content = generate_thin_caller("changelog.yaml", paths_spec=spec)
    data = yaml.safe_load(content)
    triggers = workflow_triggers(data)
    assert "install.sh" in triggers["push"]["paths"]


def test_thin_caller_paths_spec_ignore_strips_canonical() -> None:
    """Thin caller strips `ignore_paths` entries from every paths block."""
    spec = PathsSpec(ignore_paths=["uv.lock"])
    content = generate_thin_caller("changelog.yaml", paths_spec=spec)
    data = yaml.safe_load(content)
    triggers = workflow_triggers(data)
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
    triggers = workflow_triggers(data)
    assert triggers["push"]["paths"] == ["only.sh", "just-this.toml"]


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


@pytest.mark.parametrize(
    ("filename", "expected"),
    (
        # Every autofix job that writes opens a pull request; setup-guide adds
        # the issues scope.
        (
            "autofix.yaml",
            {"contents": "write", "issues": "write", "pull-requests": "write"},
        ),
        # The labellers only read the tree, so contents stays at read while the
        # scopes they do write are unioned in.
        (
            "labels.yaml",
            {"contents": "read", "issues": "write", "pull-requests": "write"},
        ),
        # Every lint job inherits the canonical top-level `permissions: {}`, so
        # a caller has nothing to forward.
        ("lint.yaml", {}),
    ),
)
def test_canonical_caller_permissions(filename: str, expected: dict[str, str]) -> None:
    """Union the job-level scopes a caller has to forward."""
    assert canonical_caller_permissions(filename) == expected


@pytest.mark.parametrize(
    ("levels", "expected"),
    (
        (("read", "write"), "write"),
        (("write", "read"), "write"),
        (("none", "read"), "read"),
        (("read", "none"), "read"),
        (("read", "read"), "read"),
        (("write", "write"), "write"),
    ),
)
def test_canonical_caller_permissions_keeps_most_permissive(
    monkeypatch: pytest.MonkeyPatch, levels: tuple[str, str], expected: str
) -> None:
    """Resolve a scope granted at odds across jobs to its strongest level.

    Order of appearance must not matter: a narrower grant later in the file
    cannot walk back a wider one already collected.
    """
    first, second = levels
    stub = (
        "---\njobs:\n"
        f"  a:\n    permissions:\n      contents: {first}\n"
        f"  b:\n    permissions:\n      contents: {second}\n"
    )
    monkeypatch.setattr(
        "repomatic.github.workflow_sync.get_data_content", lambda _name: stub
    )
    # The function memoizes per filename, so each parametrization's stub needs
    # a clean slate under the shared "stub.yaml" key.
    canonical_caller_permissions.cache_clear()
    assert canonical_caller_permissions("stub.yaml") == {"contents": expected}


def test_generate_thin_caller_omits_permissions_by_default() -> None:
    """Leave a caller with no extra jobs exactly as before.

    Its single managed job inherits the repository default, which the reusable
    workflow's own `permissions:` blocks then cap. Pinning `{}` here would buy
    nothing and starve that call.
    """
    content = generate_thin_caller("autofix.yaml", DEFAULT_REPO, "v1.2.3")
    assert "permissions" not in content
    assert yaml.safe_load(content).get("permissions") is None


def test_generate_thin_caller_emits_both_permission_halves() -> None:
    """Pin least privilege at the top and forward the scopes the call needs."""
    content = generate_thin_caller(
        "autofix.yaml", DEFAULT_REPO, "v1.2.3", with_permissions=True
    )
    data = yaml.safe_load(content)
    assert data["permissions"] == {}
    assert data["jobs"]["autofix"]["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    # The permissions block must not displace the call's own arguments.
    assert data["jobs"]["autofix"]["uses"].startswith(DEFAULT_REPO)
    assert "REPOMATIC_PAT" in data["jobs"]["autofix"]["secrets"]


def test_generate_thin_caller_spells_out_an_empty_forward() -> None:
    """Emit `permissions: {}` on the job when no upstream job asks for a scope."""
    content = generate_thin_caller(
        "lint.yaml", DEFAULT_REPO, "v1.2.3", with_permissions=True
    )
    data = yaml.safe_load(content)
    assert data["permissions"] == {}
    assert data["jobs"]["lint"]["permissions"] == {}


def test_thin_caller_permissions_track_extra_jobs(tmp_path: Path) -> None:
    """Add the contract when extra jobs appear, drop it when they go away."""
    target = tmp_path / "autofix.yaml"

    # First sync, no downstream file yet: a bare thin caller.
    sync_caller("autofix.yaml", target)
    assert yaml.safe_load(target.read_text(encoding="UTF-8")).get("permissions") is None

    # Author adds a custom job, then re-syncs.
    target.write_text(
        target.read_text(encoding="UTF-8")
        + "\n  custom:\n    runs-on: ubuntu-slim\n    steps:\n      - run: echo hi\n",
        encoding="UTF-8",
    )
    sync_caller("autofix.yaml", target)
    data = yaml.safe_load(target.read_text(encoding="UTF-8"))
    assert data["permissions"] == {}
    assert data["jobs"]["autofix"]["permissions"]["contents"] == "write"
    assert data["jobs"]["custom"]["steps"] == [{"run": "echo hi"}]

    # Custom job removed: the contract goes with it.
    target.write_text(
        generate_thin_caller("autofix.yaml", DEFAULT_REPO, "v1.2.3"), encoding="UTF-8"
    )
    sync_caller("autofix.yaml", target)
    assert yaml.safe_load(target.read_text(encoding="UTF-8")).get("permissions") is None


_PACKER_JOB = (
    "\n  packer:\n    runs-on: ubuntu-slim\n    steps:\n      - run: echo pack\n"
)
"""A downstream asset-building job, appended below the managed lanes."""


def _downstream_release(needs: list[str]) -> str:
    """A downstream `release.yaml` carrying {data}`_PACKER_JOB` and *needs*.

    Rewrites the `release` lane's `needs:` only: `publish-pypi` declares the
    same `needs: build` earlier in the file, so a blind replace would hit it.
    """
    content = generate_thin_caller("release.yaml", DEFAULT_REPO, "v1.2.3")
    head, marker, tail = content.partition("  release:\n")
    block = "    needs:\n" + "".join(f"      - {name}\n" for name in needs)
    return head + marker + tail.replace("    needs: build\n", block, 1) + _PACKER_JOB


def test_release_caller_always_denies_permissions(tmp_path: Path) -> None:
    """The canonical entry's deny-by-default reaches every downstream copy.

    Unlike a plain thin caller, whose contract only appears once extra jobs do,
    `release.yaml` is copied from an entry that always carries the key.
    """
    target = tmp_path / "release.yaml"

    sync_caller("release.yaml", target)
    assert yaml.safe_load(target.read_text(encoding="UTF-8"))["permissions"] == {}

    # A downstream job appended below the managed lanes must not loosen it.
    target.write_text(
        target.read_text(encoding="UTF-8") + _PACKER_JOB, encoding="UTF-8"
    )
    sync_caller("release.yaml", target)
    data = yaml.safe_load(target.read_text(encoding="UTF-8"))
    assert data["permissions"] == {}
    assert "packer" in data["jobs"]


@pytest.mark.parametrize(
    ("downstream_needs", "expected"),
    (
        pytest.param(["build", "packer"], ["build", "packer"], id="extra-edge-kept"),
        pytest.param(["build"], "build", id="canonical-only-stays-scalar"),
        pytest.param(["build", "ghost"], "build", id="stale-edge-dropped"),
        pytest.param(["build", "publish-pypi"], "build", id="managed-lane-not-echoed"),
    ),
)
def test_release_caller_preserves_downstream_needs(
    tmp_path: Path, downstream_needs: list[str], expected: str | list[str]
) -> None:
    """Carry the consumer's own `needs:` edges across a sync.

    A repo building a `release-assets` file gates the engine on the job packing
    it. That edge sits on a lane this renderer regenerates, so without carrying
    it over every `repomatic init` would silently drop the gate. Edges naming a
    managed lane or a vanished job are dropped instead of echoed back.
    """
    target = tmp_path / "release.yaml"
    target.write_text(_downstream_release(downstream_needs), encoding="UTF-8")

    sync_caller("release.yaml", target)

    data = yaml.safe_load(target.read_text(encoding="UTF-8"))
    assert data["jobs"]["release"]["needs"] == expected
    # The packing job itself always survives, whatever the edge resolved to.
    assert "packer" in data["jobs"]


def test_release_caller_drops_repomatic_local_needs() -> None:
    """A canonical `needs:` edge on a repomatic-only job never reaches downstream.

    The canonical entry gates its engine lane on `pack-plugin`, a job that exists
    only upstream: the renderer copies the three managed lanes and leaves it
    behind. Echoing the edge would emit a `needs:` on a job the generated file
    does not define, which GitHub rejects at startup, breaking the release
    workflow of every consumer at once.
    """
    generated = generate_thin_caller("release.yaml", DEFAULT_REPO, "v1.2.3")
    data = yaml.safe_load(generated)

    canonical = yaml.safe_load(get_data_content("release.yaml"))
    local_jobs = set(canonical["jobs"]) - set(data["jobs"])
    assert local_jobs, (
        "The canonical release.yaml no longer holds a repomatic-only job, so this "
        "test has stopped covering anything. Drop it, or gate it on a new one."
    )

    needs = data["jobs"]["release"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert not local_jobs.intersection(needs), (
        f"Generated release.yaml gates `release` on {sorted(local_jobs.intersection(needs))}, "
        "which it does not define."
    )
    # Every surviving edge names a job the generated file actually has.
    assert set(needs) <= set(data["jobs"])


def test_generated_caller_with_extra_jobs_satisfies_lint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the loop the warning opened.

    `check_workflow_permissions` is what flagged these callers in the first
    place, and it fails both on a missing top-level key and on a managed call
    starved by an empty one. A generated caller must clear both at once.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    target = workflows / "autofix.yaml"
    target.write_text(
        generate_thin_caller("autofix.yaml", DEFAULT_REPO, "v1.2.3")
        + "\n  custom:\n    runs-on: ubuntu-slim\n    steps:\n      - run: echo hi\n",
        encoding="UTF-8",
    )
    sync_caller("autofix.yaml", target)

    monkeypatch.chdir(tmp_path)
    assert [r for r in check_workflow_permissions() if r.passed is False] == []


def _thin_caller_call_sites() -> set[tuple[str, str]]:
    """Every production call of `generate_thin_caller`, as (module, function).

    Walks the AST rather than grepping, so a call spread over several lines or
    reached through an alias is still seen.
    """
    package = Path(__file__).parent.parent / "repomatic"
    sites: set[tuple[str, str]] = set()
    for module in sorted(package.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="UTF-8"))
        for parent in ast.walk(tree):
            if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(parent):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "generate_thin_caller":
                    sites.add((module.name, parent.name))
    return sites


def test_thin_caller_rendering_has_a_single_seam() -> None:
    """Only `render_thin_caller_for_target` may render a caller for a file.

    That helper is where the downstream state a sync must carry over (extra
    jobs, the consumer's own `needs:` edges) is read and passed on. A second
    call site is a second place to forget one, which is exactly how the
    `needs:` edge came to be dropped on the `repomatic init` path while the
    suite driving the other copy stayed green.

    Reach the renderer through the helper. If a caller genuinely needs raw
    output with no file behind it, it is not a sync and should say so here.
    """
    assert _thin_caller_call_sites() == {
        ("workflow_sync.py", "render_thin_caller_for_target")
    }
