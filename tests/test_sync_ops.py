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

"""Conformance tests for the dependency-updater registry.

Each test enumerates {data}`~repomatic.sync_ops.SYNC_OPERATIONS` and asserts a
shared invariant, so a new operation that breaks a convention fails by name.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from repomatic.cli import repomatic
from repomatic.config import Config
from repomatic.github.pr_body import get_template_names
from repomatic.init_project import get_data_content
from repomatic.registry import BUNDLED_VERBATIM_TARGETS, COMPONENTS, BundledComponent
from repomatic.sync_ops import (
    DEPENDENCY_LABEL,
    SYNC_OPERATIONS,
    ResolveContext,
    SyncOperation,
    SyncPlan,
    _resolve_action_pins,
    _resolve_tool_versions,
    _widest_changes,
    operation_order,
    render_plan_markdown,
    run_sync_operations,
    selected_operations,
)

OPERATION_NAMES = [op.name for op in SYNC_OPERATIONS]

AUTOFIX_WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "autofix.yaml"
)


def test_sync_deps_and_family_are_registered() -> None:
    """`sync-deps` and every updater it drives are CLI commands."""
    commands = repomatic.commands
    assert "sync-deps" in commands
    for name in OPERATION_NAMES:
        assert name in commands, f"{name} is not a registered command"


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_operation_identity_is_coherent(op: SyncOperation) -> None:
    """Command, branch, and template names are identical (naming rule 3)."""
    assert op.branch == op.name
    assert op.template == op.name
    assert op.name in repomatic.commands


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_operation_has_pr_body_template(op: SyncOperation) -> None:
    """Each updater ships a PR-body template under its own name."""
    assert op.template in get_template_names()


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_operation_config_flag_exists(op: SyncOperation) -> None:
    """Each `config_flag` names a real boolean field on the Config schema."""
    fields = {f.name for f in dataclasses.fields(Config)}
    assert op.config_flag in fields
    assert isinstance(getattr(Config(), op.config_flag), bool)


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_operation_job_name_is_descriptive(op: SyncOperation) -> None:
    """Each CI job name embeds the operation's bare noun and an emoji."""
    assert op.job_name
    # Job names lead with an emoji, so the first character is non-ASCII.
    assert ord(op.job_name[0]) > 127


def test_label_is_shared() -> None:
    """All updaters share one dependency label, so one filter finds them."""
    assert DEPENDENCY_LABEL == "🔗 dependencies"


def test_write_domains_justify_serial_apply() -> None:
    """Three updaters share the workflow files; `sync-uv-lock` is disjoint.

    This is the invariant behind the serial apply phase: the workflow-file
    rewriters cannot write concurrently, while `sync-uv-lock` (`uv.lock`,
    `pyproject.toml`) is isolated and so resolves in parallel.
    """
    by_name = {op.name: op for op in SYNC_OPERATIONS}

    def touches_workflows(op: SyncOperation) -> bool:
        return any(".github/workflows" in entry for entry in op.write_domain)

    workflow_writers = {op.name for op in SYNC_OPERATIONS if touches_workflows(op)}
    assert workflow_writers == {
        "sync-action-pins",
        "sync-workflow-pins",
        "sync-tool-versions",
    }
    assert not touches_workflows(by_name["sync-uv-lock"])


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_is_enabled_reads_the_config_flag(op: SyncOperation) -> None:
    """`is_enabled` mirrors the operation's Config boolean."""
    enabled = Config()
    setattr(enabled, op.config_flag, True)
    assert op.is_enabled(enabled)
    disabled = Config()
    setattr(disabled, op.config_flag, False)
    assert not op.is_enabled(disabled)


def test_selected_operations_drops_disabled() -> None:
    """A disabled flag removes its operation; `here_only=False` skips probes."""
    config = Config()
    config.action_pins_sync = False
    selected = selected_operations(config, here_only=False)
    names = {op.name for op in selected}
    assert "sync-action-pins" not in names
    assert "sync-uv-lock" in names


def test_selected_operations_by_name_runs_a_subset_in_registry_order() -> None:
    """Naming operations restricts to them, in registry order, not input order."""
    selected = selected_operations(Config(), names=["sync-action-pins", "sync-uv-lock"])
    # sync-uv-lock precedes sync-action-pins in the registry, regardless of input.
    assert [op.name for op in selected] == ["sync-uv-lock", "sync-action-pins"]


def test_selected_operations_by_name_still_honors_config_flags() -> None:
    """A named-but-disabled operation is dropped (feature flags are authoritative)."""
    config = Config()
    config.uv_lock_sync = False
    selected = selected_operations(config, names=["sync-uv-lock", "sync-action-pins"])
    assert [op.name for op in selected] == ["sync-action-pins"]


def _stub_operation(
    name: str, applied: list[str], *, fail: bool = False
) -> SyncOperation:
    """Build a network-free operation that records its apply order."""

    def resolve(rc: ResolveContext) -> SyncPlan:
        if fail:
            raise RuntimeError(f"{name} boom")
        return SyncPlan(
            operation=name,
            subject="Item",
            heading="Updated items",
            changes=[("thing", "1.0", "2.0")],
        )

    def apply(plan: SyncPlan) -> None:
        applied.append(name)

    return SyncOperation(
        name=name,
        config_flag="uv_lock_sync",
        job_name=f"🧪 {name}",
        job_if="",
        resolve=resolve,
        apply=apply,
        applies_here=lambda: True,
        write_domain=(name,),
    )


def test_run_sync_operations_applies_in_input_order() -> None:
    """Resolves run concurrently; applies run in the order passed in."""
    applied: list[str] = []
    ops = [_stub_operation(name, applied) for name in ("a", "b", "c")]
    rc = ResolveContext(config=Config(), today=date(2026, 1, 1))
    results = run_sync_operations(ops, rc)
    assert applied == ["a", "b", "c"]
    assert [op.name for op, _ in results] == ["a", "b", "c"]
    assert all(plan is not None and plan.has_changes for _, plan in results)


def test_run_sync_operations_dry_run_skips_apply() -> None:
    """`dry_run` resolves but never applies."""
    applied: list[str] = []
    ops = [_stub_operation(name, applied) for name in ("a", "b")]
    rc = ResolveContext(config=Config(), today=date(2026, 1, 1), dry_run=True)
    results = run_sync_operations(ops, rc)
    assert applied == []
    assert all(plan is not None for _, plan in results)


def test_run_sync_operations_isolates_a_failing_resolve() -> None:
    """One operation's resolve failure yields a `None` plan, not a crash."""
    applied: list[str] = []
    ops = [
        _stub_operation("a", applied),
        _stub_operation("b", applied, fail=True),
        _stub_operation("c", applied),
    ]
    rc = ResolveContext(config=Config(), today=date(2026, 1, 1))
    results = run_sync_operations(ops, rc)
    plans = {op.name: plan for op, plan in results}
    assert plans["b"] is None
    assert plans["a"] is not None
    assert plans["c"] is not None
    # The failed operation is never applied; the others still are.
    assert applied == ["a", "c"]


def test_operation_order_follows_the_registry() -> None:
    """`operation_order` restores canonical order regardless of input order."""
    shuffled = list(reversed(SYNC_OPERATIONS))
    assert operation_order(shuffled) == list(SYNC_OPERATIONS)


def test_sync_deps_reports_nothing_to_do_in_an_empty_tree() -> None:
    """With no lockfile, workflows, or source, no updater applies."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(repomatic, ["sync-deps", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "No dependency updaters are enabled" in result.output


def _consolidated_job_steps() -> list[dict]:
    workflow = yaml.safe_load(AUTOFIX_WORKFLOW.read_text(encoding="UTF-8"))
    steps: list[dict] = workflow["jobs"]["sync-deps"]["steps"]
    return steps


def _pr_steps(steps: list[dict]) -> list[dict]:
    return [step for step in steps if "create-pull-request" in step.get("uses", "")]


def test_consolidated_job_covers_every_operation() -> None:
    """The autofix `sync-deps` job has a step group per registered operation.

    The drift guard: a new {class}`~repomatic.sync_ops.SyncOperation` added to
    {data}`~repomatic.sync_ops.SYNC_OPERATIONS` without wiring its sync command,
    PR body, and PR into the consolidated CI job fails here by name.
    """
    steps = _consolidated_job_steps()
    runs = " ".join(step.get("run", "") for step in steps)
    pr_branches = {step["with"]["branch"] for step in _pr_steps(steps)}
    for op in SYNC_OPERATIONS:
        assert f"repomatic {op.name}" in runs, f"{op.name} sync command missing"
        assert f"--template {op.template}" in runs, f"{op.template} pr-body missing"
        assert op.branch in pr_branches, f"{op.name} create-pull-request missing"
    # No stray dependency PRs beyond the registry, in either direction.
    assert pr_branches == {op.branch for op in SYNC_OPERATIONS}


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_consolidated_job_passes_ci_flags(op: SyncOperation) -> None:
    """Each operation's `ci_flags` reach its sync step in the autofix job.

    The drift guard: an updater that declares a CI capability (like
    `--release-notes`) whose consolidated-job step never passes it silently
    drops the feature from the PR body. Fails by name when the registry and the
    YAML disagree.
    """
    steps = _consolidated_job_steps()
    sync_runs = [
        run
        for step in steps
        if f"repomatic {op.name} " in (run := step.get("run", ""))
        and "pr-body" not in run
    ]
    assert len(sync_runs) == 1, f"expected exactly one sync step for {op.name}"
    for flag in op.ci_flags:
        assert flag in sync_runs[0], f"{op.name} step is missing {flag!r}"


def test_consolidated_job_uses_full_history() -> None:
    """The job checks out full history so repeated create-pull-request calls in
    one checkout do not trip git's "shallow file has changed" race."""
    steps = _consolidated_job_steps()
    checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_consolidated_job_labels_every_pr_consistently() -> None:
    """Every PR the consolidated job opens carries the shared dependency label."""
    labels = {step["with"]["labels"] for step in _pr_steps(_consolidated_job_steps())}
    assert labels == {DEPENDENCY_LABEL}


def test_consolidated_job_resets_tree_before_each_operation() -> None:
    """Each operation resets the working tree first, so PR diffs never bleed."""
    resets = [
        step
        for step in _consolidated_job_steps()
        if step.get("run", "").strip() == "git checkout -- ."
    ]
    assert len(resets) == len(SYNC_OPERATIONS)


def test_no_standalone_dependency_jobs_remain() -> None:
    """The four bumpers live only in the consolidated job, not as separate jobs."""
    workflow = yaml.safe_load(AUTOFIX_WORKFLOW.read_text(encoding="UTF-8"))
    jobs = set(workflow["jobs"])
    assert "sync-deps" in jobs
    assert not (jobs & {op.name for op in SYNC_OPERATIONS})


def test_sync_tool_versions_routes_npm_tools_to_the_npm_registry(monkeypatch) -> None:
    """npm-backed tools resolve versions through the npm registry datasource.

    awesome-lint is the only npm tool, so recording the argument `npm_candidates`
    receives proves the `elif spec.npm` branch routes it away from PyPI/GitHub.
    Empty candidate lists mean nothing is bumped, so `tool_runner.py` stays put.
    """
    seen = {}

    def fake_npm(package):
        seen["package"] = package
        return []

    monkeypatch.setattr("repomatic.sync_ops.npm_candidates", fake_npm)
    monkeypatch.setattr("repomatic.sync_ops.pypi_candidates", lambda name: [])
    monkeypatch.setattr(
        "repomatic.sync_ops.github_candidates", lambda *args, **kwargs: []
    )

    plan = _resolve_tool_versions(
        ResolveContext(config=Config(), today=date(2026, 1, 1))
    )

    assert seen["package"] == "awesome-lint"
    assert not plan.has_changes


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        # No changes pass through empty.
        ([], []),
        # A single range is untouched.
        (
            [("owner/repo", "v1.0.0", "v2.0.0")],
            [("owner/repo", "v1.0.0", "v2.0.0")],
        ),
        # Exact per-file duplicates collapse into one row.
        (
            [("owner/repo", "v1.0.0", "v2.0.0"), ("owner/repo", "v1.0.0", "v2.0.0")],
            [("owner/repo", "v1.0.0", "v2.0.0")],
        ),
        # Mixed starting pins keep the lowest, subsuming the narrower range.
        (
            [("owner/repo", "v6.0.3", "v7.0.0"), ("owner/repo", "v6.0.2", "v7.0.0")],
            [("owner/repo", "v6.0.2", "v7.0.0")],
        ),
        # Version ordering, not lexicographic: v6.0.9 is lower than v6.0.10.
        (
            [("owner/repo", "v6.0.10", "v7.0.0"), ("owner/repo", "v6.0.9", "v7.0.0")],
            [("owner/repo", "v6.0.9", "v7.0.0")],
        ),
        # Bare workflow-literal versions compare the same as v-prefixed refs.
        (
            [("mango", "1.10.0", "2.0.0"), ("mango", "1.2.0", "2.0.0")],
            [("mango", "1.2.0", "2.0.0")],
        ),
        # Distinct names each keep their own range, sorted by name.
        (
            [("kiwi/kiwi", "v1.1.0", "v2.0.0"), ("fig/fig", "v1.0.0", "v2.0.0")],
            [("fig/fig", "v1.0.0", "v2.0.0"), ("kiwi/kiwi", "v1.1.0", "v2.0.0")],
        ),
    ),
)
def test_widest_changes(
    changes: list[tuple[str, str, str]],
    expected: list[tuple[str, str, str]],
) -> None:
    """A name repeated across files collapses onto its lowest starting version."""
    assert _widest_changes(changes) == expected


def test_bypass_only_plan_counts_as_changed_and_renders() -> None:
    """A run that only edits cooldown bypasses still produces a PR report.

    Pruning or freezing an `exclude-newer-package` entry rewrites
    `pyproject.toml` even when no package version moves; without this the
    sync PR would carry that hunk with an empty body.
    """
    plan = SyncPlan(
        operation="sync-uv-lock",
        subject="Package",
        heading="Updated packages",
        pruned_bypasses=["mango"],
    )
    assert plan.has_changes
    body = render_plan_markdown(plan)
    assert "### ❄️ Cooldown bypasses" in body
    assert "[mango](https://pypi.org/project/mango/)" in body


@pytest.mark.parametrize("in_source_repo", (False, True))
def test_resolve_action_pins_and_init_owned_files(
    monkeypatch, tmp_path, in_source_repo: bool
) -> None:
    """`sync-action-pins` skips init-owned files downstream, bumps them upstream.

    The `publish-pypi` composite action is copied byte-for-byte from a bundled
    template, so downstream its `setup-uv` pin is owned by `repomatic init` and a
    bump would ping-pong with the next `sync-repomatic`. In the source repo the
    bundled template is a symlink to this very file, so the pin is a normal
    source-of-truth ref and stays bumpable. A downstream-authored workflow pinning
    the same action always bumps.
    """
    old_sha, new_sha = "1" * 40, "2" * 40
    pin = f"astral-sh/setup-uv@{old_sha} # v8.2.0"

    action = tmp_path / ".github/actions/publish-pypi/action.yaml"
    action.parent.mkdir(parents=True)
    action.write_text(
        f"runs:\n  using: composite\n  steps:\n    - uses: {pin}\n", encoding="UTF-8"
    )
    workflow = tmp_path / ".github/workflows/ci.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        f"jobs:\n  test:\n    steps:\n      - uses: {pin}\n", encoding="UTF-8"
    )
    if in_source_repo:
        # A minimal package tree flips `_is_source_repo` on.
        (tmp_path / "repomatic/data").mkdir(parents=True)
        (tmp_path / "repomatic/__init__.py").write_text("", encoding="UTF-8")

    latest = SimpleNamespace(version="8.3.1", ref="v8.3.1", date="2026-07-07")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("repomatic.sync_ops.github_candidates", lambda *a, **k: [])
    monkeypatch.setattr("repomatic.sync_ops.select_latest", lambda *a, **k: latest)
    monkeypatch.setattr("repomatic.sync_ops.select_held_back", lambda *a, **k: None)
    monkeypatch.setattr(
        "repomatic.sync_ops.resolve_tag_to_sha", lambda *a, **k: new_sha
    )

    ctx = ResolveContext(config=Config(), today=date(2026, 7, 15))
    plan = _resolve_action_pins(ctx)
    # Keys are repo-relative (the `.github` glob runs from the chdir'd cwd).
    writes = {path.as_posix(): content for path, content in plan.file_writes.items()}

    # The downstream-authored workflow always bumps.
    assert new_sha in writes[".github/workflows/ci.yaml"]
    # The init-owned action bumps only in the source repo (no ping-pong there).
    action_key = ".github/actions/publish-pypi/action.yaml"
    if in_source_repo:
        assert new_sha in writes[action_key]
    else:
        assert action_key not in writes


def test_bundled_pin_bearing_files_are_excluded_from_pin_scan() -> None:
    """Every init-owned file carrying a `uses:` SHA pin is excluded from bumping.

    Generic guard for the ping-pong fix: any `BundledComponent` `.github/` target
    that ships a pinned `uses:` ref (today only the `publish-pypi` action) must be
    listed in `BUNDLED_VERBATIM_TARGETS`, so `_pinnable_files` drops it and neither
    `sync-action-pins` nor `sync-workflow-pins` fights the verbatim init deploy.
    """
    pin_re = re.compile(r"uses:.*@[0-9a-f]{40}")
    pinned = [
        entry.target
        for component in COMPONENTS
        if isinstance(component, BundledComponent)
        for entry in component.files
        if entry.target.startswith(".github/")
        and entry.target.endswith((".yaml", ".yml"))
        and pin_re.search(get_data_content(entry.source))
    ]

    # Guard the test against silently scanning nothing.
    assert ".github/actions/publish-pypi/action.yaml" in pinned
    for target in pinned:
        assert target in BUNDLED_VERBATIM_TARGETS
