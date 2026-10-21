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

Each test enumerates {data}`~repomatic.deps.sync.SYNC_OPERATIONS` and asserts a
shared invariant, so a new operation that breaks a convention fails by name.
The CI jobs running the operations are described here, in {data}`CI_JOBS`, and
held against the workflows.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner
from click_extra import Context

from repomatic.bundle import get_data_content
from repomatic.cli.main import repomatic
from repomatic.components import BUNDLED_VERBATIM_TARGETS, COMPONENTS, BundledComponent
from repomatic.config import Config, config_key_paths
from repomatic.deps import sources as dep_sources
from repomatic.deps.pins import (
    SETUP_UV_SLUG,
    Candidate,
    apply_workflow_literals,
    select_latest,
)
from repomatic.deps.report import HeldBackPackage
from repomatic.deps.sync import (
    ACTIONLINT_MATCHER_URL,
    DEPENDENCY_LABEL,
    SYNC_OPERATIONS,
    BypassForecast,
    ResolveContext,
    SyncOperation,
    SyncPlan,
    UvProjectExtras,
    _apply_file_writes,
    _apply_tool_versions,
    _gate_uv_on_checksums,
    _pinned_with_packages,
    _plan_file_rewrites,
    _resolve_action_pins,
    _resolve_dep_sources,
    _resolve_tool_versions,
    _resolve_workflow_pins,
    _sync_actionlint_matcher,
    _widest_changes,
    render_plan_markdown,
    run_sync_operations,
    selected_operations,
)
from repomatic.github.labels import DEFAULT_CONTENT_RULES, DEFAULT_FILE_RULES
from repomatic.github.pr import PrOperation, PrSyncResult
from repomatic.http import FetchError
from repomatic.pypi import PyPIRelease
from repomatic.templating import PrTemplate, get_template_names
from repomatic.tooling.registry import TOOL_REGISTRY
from repomatic.versions import is_newer
from tests.conftest import load_workflow

OPERATION_NAMES = [op.name for op in SYNC_OPERATIONS]

SHARED_JOB = "sync-deps"
"""The `autofix.yaml` job most operations share a checkout in."""


@dataclass(frozen=True)
class CiJob:
    """Where CI runs one operation, as the workflows declare it."""

    write_domain: tuple[str, ...]
    """Human-readable globs the operation mutates."""

    ci_flags: tuple[str, ...]
    """Flags the CI step passes to the command."""

    workflow: str = "autofix.yaml"
    """Workflow file hosting the job."""

    job: str = SHARED_JOB
    """Job ID inside {attr}`workflow`."""

    @property
    def consolidated(self) -> bool:
        """Whether the operation shares the multi-bumper job.

        The shared job runs the operations through one `sync-deps --pr-sync`
        call, which resets the working tree before each, so the previous
        bumper's diff never bleeds into its PR. An operation with a job to
        itself starts from a clean checkout and needs no reset.
        """
        return self.job == SHARED_JOB


WORKFLOW_FILES = (".github/workflows/*.yaml", ".github/actions/**/*.yaml")

CI_JOBS: dict[str, CiJob] = {
    "sync-dep-sources": CiJob(("uv.lock", "pyproject.toml"), ("--release-notes",)),
    "sync-uv-lock": CiJob(
        ("uv.lock", "pyproject.toml [tool.uv]"), ("--release-notes",)
    ),
    "sync-action-pins": CiJob(WORKFLOW_FILES, ("--release-notes",)),
    "sync-workflow-pins": CiJob(WORKFLOW_FILES, ("--release-notes",)),
    # It rewrites this package's own source, so it runs in a workflow
    # `repomatic init` never materializes downstream.
    "sync-tool-versions": CiJob(
        ("repomatic/tooling/registry.py", "repomatic/data/actionlint-matcher.json"),
        ("--release-notes",),
        workflow="self-maintenance.yaml",
        job="sync-tool-versions",
    ),
}
"""The CI job of each operation, keyed by operation name."""


def test_every_operation_has_a_ci_job() -> None:
    """A new operation states where CI runs it, or every guard below skips it."""
    assert set(CI_JOBS) == set(OPERATION_NAMES)


def test_sync_deps_and_family_are_registered() -> None:
    """`sync-deps` and every updater it drives are CLI commands."""
    commands = repomatic.list_commands(Context(repomatic))
    assert "sync-deps" in commands
    for name in OPERATION_NAMES:
        assert name in commands, f"{name} is not a registered command"


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_operation_has_pr_body_template(op: SyncOperation) -> None:
    """Each updater ships a PR-body template under its own name (naming rule 3)."""
    assert op.name in get_template_names()


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_operation_config_key_exists(op: SyncOperation) -> None:
    """Each `config_key` names a real boolean setting of the Config schema."""
    path = config_key_paths()[op.config_key]
    node: object = Config()
    for attribute in path:
        node = getattr(node, attribute)
    assert isinstance(node, bool)


def test_label_is_shared() -> None:
    """The bumper label and the labeller's rules name the same label.

    The bumpers apply it to the PRs they open, and the labeller applies it to
    anyone else's PR touching a lockfile or `pyproject.toml`. Both spellings
    are hand-written (workflow YAML cannot import Python), so one renamed
    without the other silently splits the family across two labels, and the
    single filter this constant exists for stops finding half of it.
    """
    rule_labels = set(DEFAULT_CONTENT_RULES) | set(DEFAULT_FILE_RULES)
    assert DEPENDENCY_LABEL in rule_labels


def test_write_domains_justify_serial_apply() -> None:
    """Two updaters share the workflow files; the uv pair stays out of them.

    This is the invariant behind the serial apply phase: the workflow-file
    rewriters cannot write concurrently, while `sync-uv-lock` and
    `sync-dep-sources` (`uv.lock`, `pyproject.toml`) never touch workflow
    files and resolve in parallel with them, serialized only against each
    other through the uv-project mutex.
    """

    def touches_workflows(name: str) -> bool:
        return any(".github/workflows" in entry for entry in CI_JOBS[name].write_domain)

    workflow_writers = {name for name in OPERATION_NAMES if touches_workflows(name)}
    assert workflow_writers == {"sync-action-pins", "sync-workflow-pins"}
    assert not touches_workflows("sync-uv-lock")
    assert not touches_workflows("sync-dep-sources")


def _set_flag(config: Config, key: str, value: bool) -> None:
    """Set the `[tool.repomatic]` setting at the dotted *key*."""
    *parents, leaf = config_key_paths()[key]
    node: object = config
    for attribute in parents:
        node = getattr(node, attribute)
    setattr(node, leaf, value)


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_is_enabled_reads_the_config_key(op: SyncOperation) -> None:
    """`is_enabled` mirrors the operation's Config boolean."""
    enabled = Config()
    _set_flag(enabled, op.config_key, True)
    assert op.is_enabled(enabled)
    disabled = Config()
    _set_flag(disabled, op.config_key, False)
    assert not op.is_enabled(disabled)


def test_selected_operations_drops_disabled() -> None:
    """A disabled flag removes its operation, even when named."""
    config = Config()
    config.action_pins_sync = False
    selected = selected_operations(config, names=OPERATION_NAMES)
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
        config_key="uv-lock.sync",
        resolve=resolve,
        apply=apply,
        applies_here=lambda: True,
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


def test_sync_deps_reports_nothing_to_do_in_an_empty_tree(
    tmp_path, monkeypatch
) -> None:
    """With no lockfile, workflows, or source, no updater applies."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(repomatic, ["sync-deps", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "No dependency updaters are enabled" in result.output


def _consolidated_job_steps() -> list[dict]:
    workflow = load_workflow("autofix.yaml")
    steps: list[dict] = workflow["jobs"][SHARED_JOB]["steps"]
    return steps


def _sync_deps_run() -> str:
    """The command of the consolidated job's one updater step."""
    (run,) = (
        step["run"]
        for step in _consolidated_job_steps()
        if "repomatic sync-deps" in step.get("run", "")
    )
    return str(run)


def _sync_deps_operations() -> list[str]:
    """The updaters the consolidated step names, in the order it names them."""
    return [token for token in _sync_deps_run().split() if token in OPERATION_NAMES]


def _host_job_steps(op: SyncOperation) -> list[dict]:
    """Steps of the job that actually runs *op*, per {data}`CI_JOBS`.

    Most operations share the consolidated `sync-deps` job, but one whose write
    domain exists only upstream gets its own job in a self-maintenance workflow.
    """
    ci_job = CI_JOBS[op.name]
    workflow = load_workflow(ci_job.workflow)
    steps: list[dict] = workflow["jobs"][ci_job.job]["steps"]
    return steps


_PR_SYNC_TEMPLATE_RE = re.compile(r"--template (?P<template>\S+)")


def _pr_steps(steps: list[dict]) -> list[dict]:
    """Return the steps that open, refresh or retire a pull request."""
    return [step for step in steps if "repomatic pr-sync" in step.get("run", "")]


def _pr_template(step: dict) -> str:
    """Return the template a `pr-sync` step renders."""
    match = _PR_SYNC_TEMPLATE_RE.search(step["run"])
    assert match, f"no --template in {step['run']!r}"
    return match.group("template")


def _pr_branch(step: dict) -> str:
    """Return the head branch a `pr-sync` step targets.

    Mirrors the CLI's resolution: an explicit `--branch` wins, else the branch
    defaults to the template name.
    """
    match = re.search(r"--branch (?P<branch>\S+)", step["run"])
    return match.group("branch") if match else _pr_template(step)


def _pr_labels(step: dict) -> set[str]:
    """Return the labels a `pr-sync` step's pull request carries.

    Labels live in the template's frontmatter now, so an explicit `--label`
    override wins and the template's declaration is the default, exactly as
    the CLI resolves them.
    """
    explicit = set(re.findall(r'--label "(?P<label>[^"]+)"', step["run"]))
    return explicit or set(PrTemplate.load(_pr_template(step)).labels)


@pytest.mark.repo_maintenance
def test_consolidated_job_covers_every_operation() -> None:
    """Every registered operation has a step group in the job hosting it.

    The drift guard: a new {class}`~repomatic.deps.sync.SyncOperation` added to
    {data}`~repomatic.deps.sync.SYNC_OPERATIONS` without wiring its sync command,
    PR body, and PR into a CI job fails here by name. Each operation is checked
    against its own entry in {data}`CI_JOBS`, so moving one out of the
    consolidated job keeps the guard rather than dropping it. The command, the
    PR-body template and the PR branch all share the operation's name.
    """
    for op in SYNC_OPERATIONS:
        if CI_JOBS[op.name].consolidated:
            # `--pr-sync` renders the template and targets the branch of the
            # operation's own name.
            assert op.name in _sync_deps_operations(), f"{op.name} not run"
            continue
        steps = _host_job_steps(op)
        runs = " ".join(step.get("run", "") for step in steps)
        pr_branches = {_pr_branch(step) for step in _pr_steps(steps)}
        assert f"repomatic {op.name}" in runs, f"{op.name} sync command missing"
        assert f"--template {op.name}" in runs, f"{op.name} pr-body missing"
        assert op.name in pr_branches, f"{op.name} pr-sync step missing"

    # No stray dependency PRs beyond the registry, in either direction.
    assert set(_sync_deps_operations()) == {
        name for name, ci_job in CI_JOBS.items() if ci_job.consolidated
    }
    assert "--pr-sync" in _sync_deps_run()
    assert not _pr_steps(_consolidated_job_steps())


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
@pytest.mark.repo_maintenance
def test_consolidated_job_passes_ci_flags(op: SyncOperation) -> None:
    """Each operation's `ci_flags` reach its sync step in the autofix job.

    The drift guard: an updater that declares a CI capability (like
    `--release-notes`) whose consolidated-job step never passes it silently
    drops the feature from the PR body. Fails by name when the registry and the
    YAML disagree.
    """
    if CI_JOBS[op.name].consolidated:
        sync_run = _sync_deps_run()
    else:
        sync_runs = [
            run
            for step in _host_job_steps(op)
            if f"repomatic {op.name} " in (run := step.get("run", ""))
            and "pr-body" not in run
        ]
        assert len(sync_runs) == 1, f"expected exactly one sync step for {op.name}"
        (sync_run,) = sync_runs
    for flag in CI_JOBS[op.name].ci_flags:
        assert flag in sync_run, f"{op.name} step is missing {flag!r}"


def test_consolidated_job_uses_full_history() -> None:
    """The job checks out full history so repeated create-pull-request calls in
    one checkout do not trip git's "shallow file has changed" race."""
    steps = _consolidated_job_steps()
    checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
    assert checkout.get("with", {}).get("fetch-depth") == 0


@pytest.mark.repo_maintenance
def test_consolidated_job_labels_every_pr_consistently() -> None:
    """Every PR the consolidated job opens carries the shared dependency label.

    `--pr-sync` labels each pull request from the frontmatter of the template
    named after its operation.
    """
    labels = set()
    for name in _sync_deps_operations():
        labels |= set(PrTemplate.load(name).labels)
    assert labels == {DEPENDENCY_LABEL}


def _recording_operation(
    name: str, events: list[str], *, applies: bool = True, fail: bool = False
) -> SyncOperation:
    """Build a network-free operation that logs its resolve and its apply."""

    def resolve(rc: ResolveContext) -> SyncPlan:
        events.append(f"resolve {name}")
        if fail:
            raise RuntimeError(f"{name} boom")
        return SyncPlan(
            operation=name,
            subject="Package",
            heading="Updated packages",
            changes=[("kiwi", "1.0", "2.0")],
        )

    return SyncOperation(
        name=name,
        config_key="uv-lock.sync",
        resolve=resolve,
        apply=lambda plan: events.append(f"apply {name}"),
        applies_here=lambda: applies,
    )


def test_pr_sync_mode_runs_each_operation_from_a_clean_tree(monkeypatch) -> None:
    """Each operation starts from a tree put back to HEAD, then converges its PR.

    One that fails leaves its pull request alone, never stops the next one, and
    fails the command once all ran. One with nothing to act on resolves nothing,
    but still converges its pull request, so a stale one closes.
    """
    events: list[str] = []
    operations = [
        _recording_operation("sync-dep-sources", events, applies=False),
        _recording_operation("sync-uv-lock", events, fail=True),
        _recording_operation("sync-action-pins", events),
    ]
    monkeypatch.setattr(
        "repomatic.cli.deps.selected_operations", lambda config, names: operations
    )
    monkeypatch.setattr(
        "repomatic.cli.deps.git_ops.discard_changes", lambda: events.append("reset")
    )
    bodies: dict[str, str] = {}

    def upsert(**kwargs):
        events.append(f"pr {kwargs['branch']}")
        bodies[kwargs["branch"]] = kwargs["body"]
        assert list(kwargs["labels"]) == [DEPENDENCY_LABEL]
        return PrSyncResult(PrOperation.UPDATED, kwargs["branch"], number=7)

    monkeypatch.setattr("repomatic.cli.deps.upsert_pr", upsert)

    names = [op.name for op in operations]
    result = CliRunner().invoke(repomatic, ["sync-deps", "--pr-sync", *names])

    assert result.exit_code == 1, result.output
    assert events == [
        "reset",
        "pr sync-dep-sources",
        "reset",
        "resolve sync-uv-lock",
        "reset",
        "resolve sync-action-pins",
        "apply sync-action-pins",
        "pr sync-action-pins",
    ]
    assert "kiwi" in bodies["sync-action-pins"]
    assert "kiwi" not in bodies["sync-dep-sources"]
    assert "Failed: sync-uv-lock" in result.output


@pytest.mark.parametrize("flag", ("--dry-run", "--output=report.md"))
def test_pr_sync_mode_refuses_a_preview_or_a_report(flag) -> None:
    """Each report goes into its pull request: there is nothing to preview."""
    result = CliRunner().invoke(repomatic, ["sync-deps", "--pr-sync", flag])
    assert result.exit_code == 2
    assert "--pr-sync" in result.output


def test_pinned_with_packages_covers_every_registry_pin() -> None:
    """Every `package==version` in the registry reaches the bumper.

    The drift guard: a plugin set added to a tool's `with_packages` but never
    scanned ages out silently, which is how mdformat's own `ruff==` pin sat four
    minor versions behind the top-level ruff.
    """
    collected = dict(_pinned_with_packages())
    for spec in TOOL_REGISTRY.values():
        for entry in spec.with_packages:
            package, _, version = entry.partition("==")
            assert package in collected, f"{entry} never reaches sync-tool-versions"
            # The lowest pin wins, so any tool trailing the latest still bumps.
            assert not is_newer(collected[package], version)


def test_pinned_with_packages_keeps_the_lowest_of_two_pins(monkeypatch) -> None:
    """Two tools pinning one package resolve against the older of the two."""
    papaya = dataclasses.replace(
        TOOL_REGISTRY["mdformat"], with_packages=("kiwi==2.0.0",)
    )
    mango = dataclasses.replace(
        TOOL_REGISTRY["mdformat"], with_packages=("kiwi==1.0.0",)
    )
    monkeypatch.setattr(
        "repomatic.deps.sync.TOOL_REGISTRY", {"papaya": papaya, "mango": mango}
    )
    assert _pinned_with_packages() == [("kiwi", "1.0.0")]


def test_no_standalone_dependency_jobs_remain() -> None:
    """The four bumpers live only in the consolidated job, not as separate jobs."""
    workflow = load_workflow("autofix.yaml")
    jobs = set(workflow["jobs"])
    assert SHARED_JOB in jobs
    assert not (jobs & {op.name for op in SYNC_OPERATIONS})


def test_sync_tool_versions_routes_npm_tools_to_the_npm_registry(monkeypatch) -> None:
    """npm-backed tools resolve versions through the npm registry datasource.

    Recording the arguments `npm_candidates` receives proves `tool_candidates`
    routes every npm tool away from PyPI/GitHub. Empty candidate lists mean
    nothing is bumped, so `tooling/registry.py` stays put.
    """
    seen: set[str] = set()

    def fake_npm(package):
        seen.add(package)
        return []

    # The tool pass reads through `tool_candidates`, and the `with_packages`
    # pass through the PyPI ecosystem: both resolve in `pins`.
    monkeypatch.setattr("repomatic.deps.pins.npm_candidates", fake_npm)
    monkeypatch.setattr("repomatic.deps.pins.pypi_candidates", lambda _: [])
    monkeypatch.setattr(
        "repomatic.deps.pins.github_candidates", lambda *args, **kwargs: []
    )

    plan = _resolve_tool_versions(
        ResolveContext(config=Config(), today=date(2026, 1, 1))
    )

    assert seen == {spec.name for spec in TOOL_REGISTRY.values() if spec.npm}
    assert seen >= {"awesome-lint", "npm"}
    assert not plan.has_changes


def test_tool_versions_plan_carries_the_jobs_width(monkeypatch) -> None:
    """A binary bump keeps the `--jobs` width for the checksum recompute.

    The apply runs with no Click context, so the width the CLI resolved has to
    travel on the plan.
    """

    def candidates(spec):
        if spec.name == "typos":
            return [Candidate("99.0.0", "2025-12-01", "v99.0.0")]
        return []

    monkeypatch.setattr("repomatic.deps.sync.tool_candidates", candidates)
    monkeypatch.setattr("repomatic.deps.pins.pypi_candidates", lambda _: [])

    plan = _resolve_tool_versions(
        ResolveContext(config=Config(), today=date(2026, 1, 1), jobs=6)
    )

    assert plan.tool_versions.binary_overrides == {"typos": "99.0.0"}
    assert plan.tool_versions.jobs == 6


def test_tool_versions_apply_downloads_with_the_jobs_width(tmp_path, monkeypatch):
    """The checksum recompute downloads as wide as the plan says."""
    registry = tmp_path / "registry.py"
    plan = SyncPlan(operation="sync-tool-versions", subject="Tool", heading="Tools")
    plan.changes = [("typos", "1.0.0", "99.0.0")]
    plan.file_writes = {registry: "rewritten"}
    plan.tool_versions.binary_overrides = {"typos": "99.0.0"}
    plan.tool_versions.jobs = 6
    recompute = Mock(return_value=[])
    monkeypatch.setattr("repomatic.deps.sync.update_registry_checksums", recompute)

    _apply_tool_versions(plan)

    recompute.assert_called_once_with(
        registry, version_overrides={"typos": "99.0.0"}, jobs=6
    )
    assert registry.read_text(encoding="UTF-8") == "rewritten"


def _bundled_matcher_in(tmp_path: Path, monkeypatch) -> Path:
    """Point the matcher refresh at a scratch copy of the package's data folder."""
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(
        "repomatic.deps.sync.tool_registry",
        SimpleNamespace(__file__=str(tmp_path / "tooling" / "registry.py")),
    )
    matcher = TOOL_REGISTRY["actionlint"].problem_matcher
    assert matcher
    return tmp_path / "data" / matcher


def test_actionlint_bump_refreshes_the_bundled_problem_matcher(tmp_path, monkeypatch):
    """A bumped actionlint brings the problem matcher of its own tag.

    The copy keeps the tabs the `format-json` job writes, so the two never
    fight over it.
    """
    target = _bundled_matcher_in(tmp_path, monkeypatch)
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return '{"problemMatcher": [{"owner": "actionlint"}]}'

    monkeypatch.setattr("repomatic.deps.sync.get_text", fetch)

    _sync_actionlint_matcher("9.9.9")

    assert fetched == [ACTIONLINT_MATCHER_URL.format(version="9.9.9")]
    assert target.read_text(encoding="UTF-8") == (
        '{\n\t"problemMatcher": [\n\t\t{\n\t\t\t"owner": "actionlint"\n\t\t}\n\t]\n}\n'
    )


def test_actionlint_bump_keeps_the_matcher_on_a_failed_fetch(tmp_path, monkeypatch):
    """A matcher that cannot be fetched leaves the bundled copy in place."""
    target = _bundled_matcher_in(tmp_path, monkeypatch)
    target.write_text("{}\n", encoding="UTF-8")

    def refuse(url):
        raise FetchError("raw.githubusercontent.com timed out")

    monkeypatch.setattr("repomatic.deps.sync.get_text", refuse)

    _sync_actionlint_matcher("9.9.9")

    assert target.read_text(encoding="UTF-8") == "{}\n"


def test_bundled_problem_matcher_is_written_like_a_refresh() -> None:
    """The bundled matcher has the shape a refresh writes, so a bump diffs cleanly."""
    matcher = TOOL_REGISTRY["actionlint"].problem_matcher
    assert matcher
    content = get_data_content(matcher)
    assert content == json.dumps(json.loads(content), indent="\t") + "\n"


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
        uv_project=UvProjectExtras(
            pruned_bypasses=[
                BypassForecast("mango", "2.0.0", "2026-07-01 (5 days ago)")
            ],
        ),
    )
    assert plan.has_changes
    body = render_plan_markdown(plan)
    assert "## ❄️ Cooldown bypasses" in body
    assert (
        "| [mango](https://pypi.org/project/mango/) | 🧹 cleared: `2.0.0` |"
        " 2026-07-01 (5 days ago) |"
    ) in body


def test_render_plan_markdown_closes_on_the_held_back_section() -> None:
    """What the run did comes first; what it left alone closes the body.

    The held-back section is the only forward-looking one, so it sits below
    the bypass table. A bypass-only run moves no version, and the old order
    opened its PR on releases it had not adopted.
    """
    plan = SyncPlan(
        operation="sync-uv-lock",
        subject="Package",
        heading="Updated packages",
        changes=[("cherry", "1.0.0", "1.1.0")],
        held_back=[
            HeldBackPackage("papaya", "3.0.0", "3.1.0", "a day ago", "in a week")
        ],
        uv_project=UvProjectExtras(
            bypass_forecasts=[
                BypassForecast("mango", "2.0.0", "2026-07-08 (in 2 days)")
            ]
        ),
    )
    body = render_plan_markdown(plan)
    assert (
        body.index("## 🆙 Updated packages")
        < body.index("## ❄️ Cooldown bypasses")
        < body.index("## ⏸️ Held back by cooldown")
    )


def test_render_plan_markdown_places_the_upgrade_invite_after_the_notes() -> None:
    """The invitation follows the notes it asks the maintainer to work through."""
    plan = SyncPlan(
        operation="sync-workflow-pins",
        subject="Package",
        heading="Updated packages",
        changes=[("repomatic", "7.14.0", "7.15.0")],
        notes_section="## 📝 Release notes\n\nmango",
        upgrade_section="## 🧭 Upgrade review\n\npapaya",
        held_back=[
            HeldBackPackage("cherry", "3.0.0", "3.1.0", "a day ago", "in a week")
        ],
    )
    body = render_plan_markdown(plan)
    assert (
        body.index("## 🆙 Updated packages")
        < body.index("## 📝 Release notes")
        < body.index("## 🧭 Upgrade review")
        < body.index("## ⏸️ Held back by cooldown")
    )


SWAP_PYPROJECT = """\
[project]
name = "basket"
version = "1.0.0"
dependencies = [
  "cherry>=1.2",
  "mango>=2.1.0.dev0",
]

[tool.uv]
exclude-newer = "1 week"
exclude-newer-package = { mango = "2026-01-05T00:00:00Z" }

[tool.uv.sources]
mango = { git = "https://github.com/acme/mango", branch = "main" }
"""
"""A project tracking `mango`'s main branch while awaiting its `2.1.0` release."""

SWAP_PRE_LOCK = """\
version = 1
requires-python = ">=3.10"

[options]
exclude-newer = "0001-01-01T00:00:00Z"
exclude-newer-span = "P1W"

[[package]]
name = "cherry"
version = "1.2.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example.test/cherry.tar.gz", upload-time = "2026-06-01T10:00:00Z" }

[[package]]
name = "mango"
version = "2.1.0.dev0"
source = { git = "https://github.com/acme/mango?branch=main#abcdef12" }
"""
"""The lock before the swap: `mango` resolved from git, no upload time."""

SWAP_POST_LOCK = """\
version = 1
requires-python = ">=3.10"

[options]
exclude-newer = "0001-01-01T00:00:00Z"
exclude-newer-span = "P1W"

[[package]]
name = "cherry"
version = "1.2.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example.test/cherry.tar.gz", upload-time = "2026-06-01T10:00:00Z" }

[[package]]
name = "mango"
version = "2.1.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example.test/mango.tar.gz", upload-time = "2026-07-10T09:00:00Z" }
"""
"""The lock a successful `uv lock` produces after the swap."""


def _swap_project(tmp_path: Path) -> Path:
    """Materialize the swap-ready project and return its lock path."""
    (tmp_path / "pyproject.toml").write_text(SWAP_PYPROJECT, encoding="UTF-8")
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text(SWAP_PRE_LOCK, encoding="UTF-8")
    return lockfile


def _mock_mango_release(monkeypatch) -> None:
    """Publish `mango` `2.1.0` on the stubbed index."""
    monkeypatch.setattr(
        dep_sources,
        "pypi_release_dates",
        lambda name: (
            {"2.1.0": PyPIRelease("2026-07-10", False, "mango")}
            if name == "mango"
            else {}
        ),
    )


def _mock_uv_lock(monkeypatch, lock_content: str | Exception) -> None:
    """Stub the `uv lock` subprocess with a canned outcome."""

    def fake_run(args, check=False, cwd=None, **kwargs):
        if isinstance(lock_content, Exception):
            raise lock_content
        Path(cwd, "uv.lock").write_text(lock_content, encoding="UTF-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("repomatic.deps.uv.subprocess.run", fake_run)


def test_resolve_dep_sources_swaps_and_freezes(tmp_path, monkeypatch) -> None:
    """The happy path: override dropped, floor tightened, release frozen."""
    lockfile = _swap_project(tmp_path)
    _mock_mango_release(monkeypatch)
    _mock_uv_lock(monkeypatch, SWAP_POST_LOCK)

    rc = ResolveContext(
        config=Config(), today=date(2026, 7, 15), held_back=False, lockfile=lockfile
    )
    plan = _resolve_dep_sources(rc)

    assert plan.has_changes
    assert [swap.release for swap in plan.uv_project.source_swaps] == ["2.1.0"]
    assert plan.changes == [("mango", "2.1.0.dev0", "2.1.0")]
    assert plan.uv_project.frozen_bypasses == ["mango"]
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="UTF-8")
    assert "mango = { git" not in pyproject
    assert '"mango>=2.1.0"' in pyproject
    # The stale freeze is replaced by a fresh cutoff with the one-day margin
    # past the adopted release's upload date.
    assert 'mango = "2026-07-12T00:00:00Z"' in pyproject
    assert lockfile.read_text(encoding="UTF-8") == SWAP_POST_LOCK
    # The report leads with the swap table and pins the fresh freeze.
    body = render_plan_markdown(plan)
    assert "## 🔀 Source swaps" in body
    assert "| [mango](https://pypi.org/project/mango/) | `main` | `2.1.0` |" in body
    assert "📌 frozen: `2.1.0`" in body


def test_resolve_dep_sources_dry_run_restores(tmp_path, monkeypatch) -> None:
    """A dry run reports the full swap but leaves the project untouched."""
    lockfile = _swap_project(tmp_path)
    _mock_mango_release(monkeypatch)
    _mock_uv_lock(monkeypatch, SWAP_POST_LOCK)

    rc = ResolveContext(
        config=Config(),
        today=date(2026, 7, 15),
        held_back=False,
        dry_run=True,
        lockfile=lockfile,
    )
    plan = _resolve_dep_sources(rc)

    assert plan.has_changes
    assert plan.changes == [("mango", "2.1.0.dev0", "2.1.0")]
    assert (tmp_path / "pyproject.toml").read_text(encoding="UTF-8") == SWAP_PYPROJECT
    assert lockfile.read_text(encoding="UTF-8") == SWAP_PRE_LOCK


@pytest.mark.parametrize(
    "lock_outcome",
    (
        subprocess.CalledProcessError(1, ("uv", "lock")),
        SWAP_PRE_LOCK,
    ),
    ids=("resolution-conflict", "unexpected-version"),
)
def test_resolve_dep_sources_is_all_or_nothing(
    tmp_path, monkeypatch, lock_outcome: str | Exception
) -> None:
    """A failed or wrong-version lock restores the project untouched."""
    lockfile = _swap_project(tmp_path)
    _mock_mango_release(monkeypatch)
    _mock_uv_lock(monkeypatch, lock_outcome)

    rc = ResolveContext(
        config=Config(), today=date(2026, 7, 15), held_back=False, lockfile=lockfile
    )
    plan = _resolve_dep_sources(rc)

    assert not plan.has_changes
    assert (tmp_path / "pyproject.toml").read_text(encoding="UTF-8") == SWAP_PYPROJECT
    assert lockfile.read_text(encoding="UTF-8") == SWAP_PRE_LOCK


def test_resolve_dep_sources_noop_before_release(tmp_path, monkeypatch) -> None:
    """With no qualifying release, the resolve never touches the project."""
    lockfile = _swap_project(tmp_path)
    monkeypatch.setattr(dep_sources, "pypi_release_dates", lambda name: {})

    def forbidden_run(*args, **kwargs):
        raise AssertionError("uv lock must not run without a ready swap")

    monkeypatch.setattr("repomatic.deps.uv.subprocess.run", forbidden_run)

    rc = ResolveContext(
        config=Config(), today=date(2026, 7, 15), held_back=False, lockfile=lockfile
    )
    plan = _resolve_dep_sources(rc)

    assert not plan.has_changes
    assert (tmp_path / "pyproject.toml").read_text(encoding="UTF-8") == SWAP_PYPROJECT


SELF_PIN_WORKFLOW = (
    'env:\n  UV_EXCLUDE_NEWER: "1 week"\n'
    "          uvx --no-progress 'repomatic==7.11.0' metadata\n"
)
"""A downstream workflow whose self-pin is current but carries no exemption."""


def _rewrite_plan(tmp_path, text: str, resolved: dict) -> tuple[SyncPlan, Path]:
    """Run `_plan_file_rewrites` over a single workflow file."""
    path = tmp_path / "tests.yaml"
    path.write_text(text, encoding="UTF-8")
    plan = SyncPlan(operation="sync-workflow-pins", subject="Pin", heading="Updated")
    rewriter = partial(
        apply_workflow_literals,
        self_pin="repomatic",
    )
    rc = ResolveContext(config=Config(), today=date(2026, 8, 14), held_back=False)
    _plan_file_rewrites(plan, rc, {path: text}, rewriter, resolved)
    return plan, path


def test_plan_file_rewrites_backfills_exemption_without_a_version_bump(tmp_path):
    """A splice-only rewrite is still a change, and still gets written.

    The exemption backfill reports no `(name, old, new)` triple, since it names
    a package rather than moving a version. Gating the write on that list threw
    the rewrite away, so a downstream repo already pinned at the newest release
    never got the flag and its whole test workflow stayed unresolvable.
    """
    plan, path = _rewrite_plan(tmp_path, SELF_PIN_WORKFLOW, {})

    assert not plan.changes
    assert plan.has_changes
    assert plan.self_pin_exemptions == ["tests.yaml"]
    assert path in plan.file_writes

    _apply_file_writes(plan)
    assert "--exclude-newer-package repomatic=P0D" in path.read_text(encoding="UTF-8")


def test_plan_file_rewrites_reports_nothing_when_already_exempt(tmp_path):
    """An exemption already in place leaves the plan empty, not endlessly dirty."""
    exempt = SELF_PIN_WORKFLOW.replace(
        "uvx --no-progress 'repomatic",
        "uvx --no-progress --exclude-newer-package repomatic=P0D 'repomatic",
    )
    plan, _path = _rewrite_plan(tmp_path, exempt, {})

    assert not plan.has_changes
    assert not plan.self_pin_exemptions
    assert not plan.file_writes


def test_plan_file_rewrites_keeps_version_bumps_out_of_the_exemption_list(tmp_path):
    """A file that also moved a version reports through `changes`, not twice."""
    plan, _path = _rewrite_plan(
        tmp_path, SELF_PIN_WORKFLOW, {("pypi", "repomatic"): "7.12.0"}
    )

    assert ("repomatic", "7.11.0", "7.12.0") in plan.changes
    assert not plan.self_pin_exemptions


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
        # A minimal package tree flips `is_source_repo` on.
        (tmp_path / "repomatic/data").mkdir(parents=True)
        (tmp_path / "repomatic/__init__.py").write_text("", encoding="UTF-8")

    latest = SimpleNamespace(version="8.3.1", ref="v8.3.1", date="2026-07-07")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("repomatic.deps.sync.github_candidates", lambda *a, **k: [])
    monkeypatch.setattr("repomatic.deps.sync.select_latest", lambda *a, **k: latest)
    monkeypatch.setattr("repomatic.deps.sync.select_held_back", lambda *a, **k: None)
    monkeypatch.setattr(
        "repomatic.deps.sync.resolve_tag_to_sha", lambda *a, **k: new_sha
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


def test_apply_file_writes_rebases_on_current_text(tmp_path):
    """A sibling operation's apply landing between plan and write survives.

    Both `.github/` pin updaters plan whole-file rewrites from the same
    pre-apply snapshot; writing the planned text verbatim would revert
    whichever sibling applied first.
    """
    target = tmp_path / "workflow.yaml"
    target.write_text("alpha: 1\nbeta: 1\n", encoding="UTF-8")

    # This plan was computed against the original text and bumps beta.
    plan = SyncPlan(operation="sync-workflow-pins", subject="X", heading="X")
    plan.file_writes[target] = "alpha: 1\nbeta: 2\n"
    plan.rebase = lambda text: (text.replace("beta: 1", "beta: 2"), [])

    # A sibling operation bumped alpha after this plan resolved.
    target.write_text("alpha: 2\nbeta: 1\n", encoding="UTF-8")

    _apply_file_writes(plan)

    assert target.read_text(encoding="UTF-8") == "alpha: 2\nbeta: 2\n"


# ---------------------------------------------------------------------------
# uv checksum gate
# ---------------------------------------------------------------------------

UV_CANDIDATES = [
    Candidate("0.12.2", "2026-08-05", "0.12.2"),
    Candidate("0.12.3", "2026-08-07", "0.12.3"),
    Candidate("0.12.4", "2026-08-13", "0.12.4"),
]
"""Three uv releases, all clear of the cooldown on `GATE_TODAY`."""

GATE_TODAY = date(2026, 8, 28)
MIN_AGE = timedelta(days=7)

SETUP_UV_PIN = {
    Path("lint.yaml"): (
        "jobs:\n  lint:\n    steps:\n"
        f"      - uses: {SETUP_UV_SLUG}@{'a' * 40} # v10.0.0\n"
        '        with:\n          version: "0.12.3"\n'
    )
}
"""A workflow pinning both halves: the action commit and the uv it installs."""


def _gate(verified, pinned="0.12.3", candidates=None):
    """Run the gate with *verified* standing in for the fetched table."""
    with patch("repomatic.deps.sync.setup_uv_verified_versions", return_value=verified):
        return _gate_uv_on_checksums(
            list(UV_CANDIDATES if candidates is None else candidates),
            pinned,
            SETUP_UV_PIN,
            MIN_AGE,
            GATE_TODAY,
        )


def test_uv_gate_drops_unverifiable_releases():
    """A uv the pinned action cannot checksum is not a candidate.

    Without this, `sync-workflow-pins` walks uv past the table on its own
    schedule and CI installs it without a pinned hash.
    """
    gated, _unverified = _gate(frozenset({"0.12.2", "0.12.3"}))
    assert [candidate.version for candidate in gated] == ["0.12.2", "0.12.3"]
    picked = select_latest(gated, MIN_AGE, GATE_TODAY)
    assert picked is not None and picked.version == "0.12.3"


def test_uv_gate_reads_the_action_pin_out_of_the_scanned_files():
    """The commit the table is fetched at comes from the files being synced.

    The gate's two halves are wired through the scanned text: miss the pin and
    it silently degrades to ungated, which is the failure it exists to prevent.
    """
    with patch(
        "repomatic.deps.sync.setup_uv_verified_versions", return_value=None
    ) as verified:
        _gate_uv_on_checksums(
            list(UV_CANDIDATES), "0.12.3", SETUP_UV_PIN, MIN_AGE, GATE_TODAY
        )
    verified.assert_called_once_with({"a" * 40})


def test_uv_gate_holds_the_pin_when_the_table_is_unknown(caplog):
    """An unreadable table keeps out every release newer than the pin.

    A blind bump can overshoot the table, and the next run that reads it steps
    the pin back again.
    """
    with caplog.at_level(logging.WARNING):
        gated, unverified = _gate(None, pinned="0.12.3")
    assert [candidate.version for candidate in gated] == ["0.12.2", "0.12.3"]
    assert unverified is False
    assert "Holding the uv pin at 0.12.3" in caplog.text


def test_uv_gate_passes_everything_through_without_a_setup_uv_pin():
    """With no action pinned there is no table to wait for, so nothing holds."""
    no_action = {
        Path("lint.yaml"): "jobs:\n  lint:\n    steps:\n      - run: uv sync\n"
    }
    with patch("repomatic.deps.sync.setup_uv_verified_versions") as verified:
        gated = _gate_uv_on_checksums(
            list(UV_CANDIDATES), "0.12.3", no_action, MIN_AGE, GATE_TODAY
        )
    assert gated == (UV_CANDIDATES, False)
    verified.assert_not_called()


def test_uv_gate_reports_a_pin_above_the_table():
    """A pin past the table is signalled back, so the caller can step it down.

    Reported rather than silently gated, because the sync only walks a version
    backwards on this one condition.
    """
    gated, unverified = _gate(frozenset({"0.12.2", "0.12.3"}), pinned="0.12.5")
    assert unverified is True
    picked = select_latest(gated, MIN_AGE, GATE_TODAY)
    assert picked is not None and picked.version == "0.12.3"


def test_uv_gate_warns_when_the_pinned_uv_is_unverified(caplog):
    """The pin on disk is audited too, not just the one about to replace it."""
    with caplog.at_level(logging.WARNING):
        _gate(frozenset({"0.11.30"}), pinned="0.12.3")
    assert "no pinned hash verifies it" in caplog.text


def test_uv_gate_stays_quiet_when_the_pinned_uv_is_verified(caplog):
    """A covered pin is the normal state and earns no warning."""
    with caplog.at_level(logging.WARNING):
        _gated, unverified = _gate(frozenset({"0.12.3", "0.12.4"}), pinned="0.12.3")
    assert not caplog.text
    assert unverified is False


def test_uv_gate_reports_a_release_it_withheld(caplog):
    """A newer uv the action cannot verify is announced, at info level.

    uv ships weekly against `setup-uv`'s monthly cadence, so this is the
    ordinary state between action bumps: a warning every run would train the
    reader to ignore the ones that mean something.
    """
    with caplog.at_level(logging.INFO):
        _gate(frozenset({"0.12.3"}), pinned="0.12.3")
    assert "0.12.4" in caplog.text
    assert "holding the pin" in caplog.text


def _resolve_uv_pin(
    pinned: str, verified: frozenset[str] | None = frozenset({"0.12.3", "0.12.4"})
) -> SyncPlan:
    """Resolve the workflow pins of a job installing uv *pinned* via `setup-uv`.

    The pinned action's checksum table covers `0.12.3` and `0.12.4`, unless
    *verified* stands in for another table, or for an unreadable one (`None`).
    """
    workflow = {
        Path("tests.yaml"): (
            "jobs:\n  tests:\n    steps:\n"
            f"      - uses: {SETUP_UV_SLUG}@{'a' * 40} # v10.0.1\n"
            f'        with:\n          version: "{pinned}"\n'
        )
    }
    rc = ResolveContext(config=Config(), today=GATE_TODAY)
    with (
        patch("repomatic.deps.sync._pinnable_files", return_value=workflow),
        patch("repomatic.deps.pins.pypi_candidates", return_value=list(UV_CANDIDATES)),
        patch("repomatic.deps.sync.setup_uv_verified_versions", return_value=verified),
    ):
        return _resolve_workflow_pins(rc)


def test_workflow_pins_step_a_uv_pin_back_onto_the_checksum_table(caplog):
    """The repair is the pin move itself, not the warning beside it.

    A repository whose uv pin overtook the table has no forward move left when
    the newest `setup-uv` is the one it already pins, so the sync walks uv down
    to the newest release that action can verify.
    """
    with caplog.at_level(logging.WARNING):
        plan = _resolve_uv_pin("0.12.5")
    assert ("uv", "0.12.5", "0.12.4") in plan.changes
    assert 'version: "0.12.4"' in plan.file_writes[Path("tests.yaml")]
    assert "Stepping the uv pin back" in caplog.text


def test_workflow_pins_label_a_uv_step_back_in_the_table():
    """A pin moving down says so in its own row, and links to why.

    The arrow alone renders a step back like any bump, so without the label
    the repair reads as an update to an older release.
    """
    plan = _resolve_uv_pin("0.12.5")
    assert (
        "| [uv](https://pypi.org/project/uv/)"
        " | [⏪ stepped back](https://repomatic.net/workflows"
        "#sync-workflow-pins-updater): `0.12.5` → `0.12.4` |"
    ) in render_plan_markdown(plan)


def test_workflow_pins_label_nothing_on_a_forward_uv_bump():
    """An ordinary uv bump inside the table carries no label."""
    plan = _resolve_uv_pin("0.12.3")
    assert ("uv", "0.12.3", "0.12.4") in plan.changes
    assert not plan.change_labels


def test_workflow_pins_hold_the_uv_pin_on_an_unreadable_table():
    """An unreadable table moves the uv pin neither forward nor back.

    `0.12.4` cleared the cooldown, so only the hold keeps it out.
    """
    plan = _resolve_uv_pin("0.12.3", verified=None)
    assert not plan.changes
    assert not plan.file_writes


def _lockstep_workflow(inline_pin: str) -> dict[Path, str]:
    """A downstream job pinning the upstream refs at `7.15.0` beside an inline pin."""
    return {
        Path("tests.yaml"): (
            "jobs:\n  build:\n    steps:\n"
            "      - uses: kdeldycke/repomatic/.github/workflows/lint.yaml"
            f"@{'a' * 40} # v7.15.0\n"
            f"      - run: uvx 'repomatic=={inline_pin}' metadata\n"
        )
    }


def test_workflow_pins_invite_the_upgrade_review_on_a_repomatic_bump() -> None:
    """A lockstep bump of the upstream pin carries the review launcher.

    The `uses:` refs already moved by hand, so realigning the inline pin is
    the pull request where a maintainer learns the repository now runs a newer
    release, and the one where both versions are known.
    """
    rc = ResolveContext(config=Config(), today=GATE_TODAY)
    with patch(
        "repomatic.deps.sync._pinnable_files", return_value=_lockstep_workflow("7.14.0")
    ):
        plan = _resolve_workflow_pins(rc)
    assert ("repomatic", "7.14.0", "7.15.0") in plan.changes
    assert "moves `repomatic` from `v7.14.0` to `v7.15.0`" in plan.upgrade_section
    assert (
        "claude --model opus '/repomatic-upgrade v7.14.0 v7.15.0'"
        in plan.upgrade_section
    )
    assert plan.upgrade_section in render_plan_markdown(plan)


def test_workflow_pins_invite_nothing_without_a_repomatic_bump() -> None:
    """An inline pin already aligned with the refs leaves the section out."""
    rc = ResolveContext(config=Config(), today=GATE_TODAY)
    with patch(
        "repomatic.deps.sync._pinnable_files", return_value=_lockstep_workflow("7.15.0")
    ):
        plan = _resolve_workflow_pins(rc)
    assert plan.upgrade_section == ""
