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
import logging
import re
import subprocess
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic import dep_sources
from repomatic.cli import repomatic
from repomatic.config import Config
from repomatic.dep_report import HeldBackPackage
from repomatic.github.pr_body import get_template_names, template_labels
from repomatic.init_project import get_data_content
from repomatic.labels import DEFAULT_CONTENT_RULES, DEFAULT_FILE_RULES
from repomatic.prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from repomatic.pypi import PyPIRelease
from repomatic.registry import BUNDLED_VERBATIM_TARGETS, COMPONENTS, BundledComponent
from repomatic.sync_ops import (
    DEPENDENCY_LABEL,
    SYNC_OPERATIONS,
    BypassForecast,
    ResolveContext,
    SyncOperation,
    SyncPlan,
    UvProjectExtras,
    _apply_file_writes,
    _gate_uv_on_checksums,
    _pinned_with_packages,
    _plan_file_rewrites,
    _resolve_action_pins,
    _resolve_dep_sources,
    _resolve_tool_versions,
    _widest_changes,
    operation_order,
    render_plan_markdown,
    run_sync_operations,
    selected_operations,
)
from repomatic.tool_registry import TOOL_REGISTRY
from repomatic.version_sync import (
    SETUP_UV_SLUG,
    Candidate,
    apply_workflow_literals,
    select_latest,
)
from repomatic.versions import is_newer
from tests.conftest import load_workflow

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
    """Three updaters share the workflow files; the uv pair stays out of them.

    This is the invariant behind the serial apply phase: the workflow-file
    rewriters cannot write concurrently, while `sync-uv-lock` and
    `sync-dep-sources` (`uv.lock`, `pyproject.toml`) never touch workflow
    files and resolve in parallel with them, serialized only against each
    other through the uv-project mutex.
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
    assert not touches_workflows(by_name["sync-dep-sources"])


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
    workflow = load_workflow("autofix.yaml")
    steps: list[dict] = workflow["jobs"]["sync-deps"]["steps"]
    return steps


def _host_job_steps(op: SyncOperation) -> list[dict]:
    """Steps of the job that actually runs *op*, per its registry entry.

    Most operations share the consolidated `sync-deps` job, but one whose write
    domain exists only upstream gets its own job in a self-maintenance workflow.
    """
    workflow = load_workflow(op.workflow)
    steps: list[dict] = workflow["jobs"][op.job]["steps"]
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
    return explicit or set(template_labels(_pr_template(step)))


def test_consolidated_job_covers_every_operation() -> None:
    """Every registered operation has a step group in the job hosting it.

    The drift guard: a new {class}`~repomatic.sync_ops.SyncOperation` added to
    {data}`~repomatic.sync_ops.SYNC_OPERATIONS` without wiring its sync command,
    PR body, and PR into a CI job fails here by name. Each operation is checked
    against its own {attr}`~repomatic.sync_ops.SyncOperation.workflow` and
    {attr}`~repomatic.sync_ops.SyncOperation.job`, so moving one out of the
    consolidated job keeps the guard rather than dropping it.
    """
    for op in SYNC_OPERATIONS:
        steps = _host_job_steps(op)
        runs = " ".join(step.get("run", "") for step in steps)
        pr_branches = {_pr_branch(step) for step in _pr_steps(steps)}
        assert f"repomatic {op.name}" in runs, f"{op.name} sync command missing"
        assert f"--template {op.template}" in runs, f"{op.template} pr-body missing"
        assert op.branch in pr_branches, f"{op.name} pr-sync step missing"

    # No stray dependency PRs beyond the registry, in either direction.
    assert {_pr_branch(step) for step in _pr_steps(_consolidated_job_steps())} == {
        op.branch for op in SYNC_OPERATIONS if op.consolidated
    }


@pytest.mark.parametrize("op", SYNC_OPERATIONS, ids=OPERATION_NAMES)
def test_consolidated_job_passes_ci_flags(op: SyncOperation) -> None:
    """Each operation's `ci_flags` reach its sync step in the autofix job.

    The drift guard: an updater that declares a CI capability (like
    `--release-notes`) whose consolidated-job step never passes it silently
    drops the feature from the PR body. Fails by name when the registry and the
    YAML disagree.
    """
    steps = _host_job_steps(op)
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
    labels = set()
    for step in _pr_steps(_consolidated_job_steps()):
        labels |= _pr_labels(step)
    assert labels == {DEPENDENCY_LABEL}


def test_consolidated_job_resets_tree_before_each_operation() -> None:
    """Each shared-job operation resets the tree first, so PR diffs never bleed.

    Only the consolidated operations need this: one with a job to itself starts
    from a clean checkout, so a reset there would be dead weight.
    """
    resets = [
        step
        for step in _consolidated_job_steps()
        if step.get("run", "").strip() == "git checkout -- ."
    ]
    assert len(resets) == len([op for op in SYNC_OPERATIONS if op.consolidated])


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
        "repomatic.sync_ops.TOOL_REGISTRY", {"papaya": papaya, "mango": mango}
    )
    assert _pinned_with_packages() == [("kiwi", "1.0.0")]


def test_no_standalone_dependency_jobs_remain() -> None:
    """The four bumpers live only in the consolidated job, not as separate jobs."""
    workflow = load_workflow("autofix.yaml")
    jobs = set(workflow["jobs"])
    assert "sync-deps" in jobs
    assert not (jobs & {op.name for op in SYNC_OPERATIONS})


def test_sync_tool_versions_routes_npm_tools_to_the_npm_registry(monkeypatch) -> None:
    """npm-backed tools resolve versions through the npm registry datasource.

    awesome-lint is the only npm tool, so recording the argument `npm_candidates`
    receives proves the `elif spec.npm` branch routes it away from PyPI/GitHub.
    Empty candidate lists mean nothing is bumped, so `tool_registry.py` stays put.
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
        "get_release_dates",
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
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("repomatic.sync_ops.subprocess.run", fake_run)


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
    monkeypatch.setattr(dep_sources, "get_release_dates", lambda name: {})

    def forbidden_run(*args, **kwargs):
        raise AssertionError("uv lock must not run without a ready swap")

    monkeypatch.setattr("repomatic.sync_ops.subprocess.run", forbidden_run)

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
        self_pin=("repomatic", SELF_PIN_COOLDOWN_EXEMPTION),
    )
    rc = ResolveContext(config=Config(), today=date(2026, 8, 14), held_back=False)
    _plan_file_rewrites(plan, rc, {path: text}, rewriter, resolved, timedelta(days=7))
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


def test_apply_file_writes_verbatim_without_rebase(tmp_path):
    """A plan carrying no rebase closure still writes its planned text."""
    target = tmp_path / "registry.py"
    target.write_text("old\n", encoding="UTF-8")

    plan = SyncPlan(operation="sync-tool-versions", subject="X", heading="X")
    plan.file_writes[target] = "new\n"

    _apply_file_writes(plan)

    assert target.read_text(encoding="UTF-8") == "new\n"


# ---------------------------------------------------------------------------
# uv checksum gate
# ---------------------------------------------------------------------------

UV_CANDIDATES = [
    Candidate("0.12.2", "2026-08-05", "0.12.2"),
    Candidate("0.12.3", "2026-08-07", "0.12.3"),
    Candidate("0.12.4", "2026-08-13", "0.12.4"),
]
"""Three uv releases, all clear of the cooldown on {data}`GATE_TODAY`."""

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
    with patch("repomatic.sync_ops.setup_uv_verified_versions", return_value=verified):
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
    schedule and CI installs it with no verification at all.
    """
    gated = _gate(frozenset({"0.12.2", "0.12.3"}))
    assert [candidate.version for candidate in gated] == ["0.12.2", "0.12.3"]
    picked = select_latest(gated, MIN_AGE, GATE_TODAY)
    assert picked is not None and picked.version == "0.12.3"


def test_uv_gate_reads_the_action_pin_out_of_the_scanned_files():
    """The commit the table is fetched at comes from the files being synced.

    The gate's two halves are wired through the scanned text: miss the pin and
    it silently degrades to ungated, which is the failure it exists to prevent.
    """
    with patch(
        "repomatic.sync_ops.setup_uv_verified_versions", return_value=None
    ) as verified:
        _gate_uv_on_checksums(
            list(UV_CANDIDATES), "0.12.3", SETUP_UV_PIN, MIN_AGE, GATE_TODAY
        )
    verified.assert_called_once_with({"a" * 40})


def test_uv_gate_passes_everything_through_when_the_table_is_unknown():
    """An unreadable table degrades to the ungated behaviour, never to a block."""
    assert _gate(None) == UV_CANDIDATES


def test_uv_gate_never_downgrades_a_pin_above_the_table():
    """A pin already past the table stays put instead of walking backwards.

    The gate only removes candidates; the caller adopts one solely when it
    beats what is pinned, so the repair stays a `sync-action-pins` bump.
    """
    gated = _gate(frozenset({"0.11.30"}), pinned="0.12.3")
    assert select_latest(gated, MIN_AGE, GATE_TODAY) is None


def test_uv_gate_warns_when_the_pinned_uv_is_unverified(caplog):
    """The pin on disk is audited too, not just the one about to replace it."""
    with caplog.at_level(logging.WARNING):
        _gate(frozenset({"0.11.30"}), pinned="0.12.3")
    assert "installs it unverified" in caplog.text


def test_uv_gate_stays_quiet_when_the_pinned_uv_is_verified(caplog):
    """A covered pin is the normal state and earns no warning."""
    with caplog.at_level(logging.WARNING):
        _gate(frozenset({"0.12.3", "0.12.4"}), pinned="0.12.3")
    assert not caplog.text


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
