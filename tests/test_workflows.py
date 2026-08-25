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
"""Tests for GitHub Actions workflow files consistency."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
import tomlrt
import yaml

from repomatic.binary import NUITKA_BUILD_TARGETS
from repomatic.config import SITE_DEPLOY_TARGETS
from repomatic.git_ops import (
    CHANGELOG_COMMIT_PREFIX,
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    RELEASE_COMMIT_PATTERN,
    RELEASE_COMMIT_PREFIX,
    VERSION_BUMP_BRANCHES,
    VERSION_BUMP_COMMIT_PREFIXES,
)
from repomatic.github.pr_body import template_labels
from repomatic.github.workflow_sync import cooldown_env_block, workflow_triggers
from repomatic.lint_repo import KNOWN_RUNNERS
from repomatic.plugin import ARCHIVE_NAME
from repomatic.prepare_release import LOCAL_CLI_INVOCATION
from repomatic.registry import (
    ALL_WORKFLOW_FILES,
    COMPONENTS,
    RELEASE_ENGINE_WORKFLOWS,
    SELF_MAINTENANCE_WORKFLOWS,
    WORKFLOW_SOURCES,
)
from repomatic.version_sync import find_workflow_literals
from tests.conftest import (
    WORKFLOWS_WITH_CONCURRENCY_BLOCK,
    WORKFLOWS_WITHOUT_CONCURRENCY_BLOCK,
    load_workflow,
)

# Commit message prefix for post-release version bump.
POST_RELEASE_COMMIT_PREFIX = f"{CHANGELOG_COMMIT_PREFIX}Post-release bump"

# Root of the repository.
REPO_ROOT = Path(__file__).parent.parent

# Path to the workflows directory.
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Workflows that are exempt from concurrency requirements.
WORKFLOWS_WITHOUT_CONCURRENCY = frozenset((
    "autolock.yaml",  # Scheduled only, no concurrent execution possible.
    "cancel-runs.yaml",  # Fires on PR close, must always run to completion.
    # Release lanes: the push-triggered release.yaml entry owns the concurrency
    # group. A block on either reusable lane would contend with the entry's group
    # and, being reached via the caller's `needs: build` gate, would join its
    # group too late to cancel queued runs.
    "_release-build.yaml",
    "_release-engine.yaml",
    "unsubscribe.yaml",  # Scheduled only, no concurrent execution possible.
))

# Workflows that protect releases using unique concurrency groups (github.sha)
# instead of conditional cancel-in-progress. This is necessary when
# cancel-in-progress is evaluated on the NEW workflow, which would cancel
# running releases. The group lives on the push-triggered entry workflow, not
# the reusable engine lane it calls (a block on a `needs:`-gated lane joins its
# group too late to cancel queued runs).
WORKFLOWS_WITH_UNIQUE_GROUPS = frozenset((
    # release.yaml uses github.sha in group for release/post-release commits.
    "release.yaml",
))

# Workflows that use event-scoped concurrency groups (github.event_name in group)
# with always-cancellable cancel-in-progress. This prevents cross-event
# cancellation without needing conditional cancel-in-progress.
WORKFLOWS_WITH_EVENT_SCOPED_GROUPS = frozenset((
    # workflow_run events from "🚀 Build & release" cancel push-triggered runs
    # without event_name in the group.
    "changelog.yaml",
))

# Workflows that must have concurrency configured (all except exempted ones).
WORKFLOWS_WITH_CONCURRENCY = tuple(
    sorted(
        p.name
        for p in WORKFLOWS_DIR.glob("*.yaml")
        if p.name not in WORKFLOWS_WITHOUT_CONCURRENCY
    )
)

# PR-triggered workflows that must skip automated version-bump PRs. These
# workflows are heavy enough that running them on bot-authored drafts whose
# code is identical to `main` is pure waste. Each lists VERSION_BUMP_BRANCHES
# under `pull_request.branches-ignore` (documentation of intent, since the
# trigger-level filter matches the PR's *base* branch, not its head) and
# gates its `metadata` job on `github.head_ref` (the actual skip mechanism).
WORKFLOWS_IGNORING_VERSION_BUMPS = frozenset((
    "labels.yaml",
    "lint.yaml",
    "tests.yaml",
))

# Workflows whose `metadata` pre-job gates downstream jobs by skipping
# itself on every VERSION_BUMP_COMMIT_PREFIXES member (including
# `[changelog] Post-release bump `). `tests.yaml` is *not* in this set
# because it must still test the post-release-bump push: that push also
# carries the release commit, and is the only post-merge test of the
# release-frozen tree. See `test_tests_metadata_gate_skips_manual_bumps`
# for tests.yaml's narrower gating. `autofix.yaml` qualifies because
# version-bump pushes are machine-generated and ship-gated, and its one
# release-triggered job (`update-dep-graph`) moved to the release engine;
# its three metadata-independent jobs repeat the commit-prefix clause on
# their own `if:`.
WORKFLOWS_WITH_METADATA_GATE = frozenset((
    "autofix.yaml",
    "labels.yaml",
    "lint.yaml",
))

# Workflows with no `push` trigger. `github.event.head_commit` is always null
# for them, so a release-commit guard in cancel-in-progress would be dead logic:
# they cancel unconditionally. Derived rather than hand-listed, so a workflow
# that later grows a push trigger falls back under the conditional-cancel rule
# without anyone remembering to update this set.
WORKFLOWS_WITHOUT_PUSH_TRIGGER = frozenset(
    p.name
    for p in WORKFLOWS_DIR.glob("*.yaml")
    if "push" not in workflow_triggers(yaml.safe_load(p.read_text(encoding="UTF-8")))
)

# Workflows that must use conditional cancel-in-progress (excludes unique
# group, event-scoped, and push-less workflows).
WORKFLOWS_WITH_CONDITIONAL_CANCEL = tuple(
    sorted(
        name
        for name in WORKFLOWS_WITH_CONCURRENCY
        if name not in WORKFLOWS_WITH_UNIQUE_GROUPS
        and name not in WORKFLOWS_WITH_EVENT_SCOPED_GROUPS
        and name not in WORKFLOWS_WITHOUT_PUSH_TRIGGER
    )
)


@pytest.mark.parametrize("workflow_name", WORKFLOWS_WITH_CONCURRENCY)
def test_workflow_has_concurrency(workflow_name: str) -> None:
    """Verify that required workflows have a concurrency block."""
    workflow = load_workflow(workflow_name)
    assert "concurrency" in workflow, f"{workflow_name} must have a concurrency block"


@pytest.mark.parametrize("workflow_name", WORKFLOWS_WITH_CONCURRENCY)
def test_concurrency_group_format(workflow_name: str) -> None:
    """Verify that concurrency group uses PR number or branch ref."""
    workflow = load_workflow(workflow_name)
    concurrency = workflow.get("concurrency", {})
    group = concurrency.get("group", "")

    # Expected format: workflow name + PR number or branch ref.
    assert "github.workflow" in group, (
        f"{workflow_name}: concurrency group must include github.workflow"
    )

    # Only require PR number in group if the workflow has a pull_request trigger.
    triggers = workflow.get("on", {})
    has_pr_trigger = "pull_request" in triggers or "pull_request_target" in triggers
    if has_pr_trigger:
        assert "github.event.pull_request.number" in group, (
            f"{workflow_name}: concurrency group must include PR number for PR events"
        )

    assert "github.ref" in group, (
        f"{workflow_name}: concurrency group must include github.ref for push events"
    )


@pytest.mark.parametrize("workflow_name", WORKFLOWS_WITH_CONDITIONAL_CANCEL)
def test_cancel_in_progress_protects_releases(workflow_name: str) -> None:
    """Verify that cancel-in-progress protects release commits."""
    workflow = load_workflow(workflow_name)
    concurrency = workflow.get("concurrency", {})
    cancel_in_progress = concurrency.get("cancel-in-progress", "")

    # Must be a conditional expression, not a static boolean.
    assert isinstance(cancel_in_progress, str), (
        f"{workflow_name}: cancel-in-progress must be a conditional expression, "
        f"not {type(cancel_in_progress).__name__}"
    )

    # Must reference the release commit prefix.
    assert RELEASE_COMMIT_PREFIX in cancel_in_progress, (
        f"{workflow_name}: cancel-in-progress must check for "
        f"'{RELEASE_COMMIT_PREFIX}' to protect release commits"
    )

    # Must use startsWith for proper prefix matching.
    assert "startsWith" in cancel_in_progress, (
        f"{workflow_name}: cancel-in-progress must use startsWith() "
        "for commit message matching"
    )

    # Must check the head commit message.
    assert "github.event.head_commit.message" in cancel_in_progress, (
        f"{workflow_name}: cancel-in-progress must check "
        "github.event.head_commit.message"
    )

    # Must negate the condition (release commits should NOT be cancelled).
    # Handle multiline YAML expressions where whitespace may appear after ${{.
    normalized = " ".join(cancel_in_progress.split())
    assert normalized.startswith("${{ !"), (
        f"{workflow_name}: cancel-in-progress must negate the condition "
        "to protect release commits from cancellation"
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS_WITH_EVENT_SCOPED_GROUPS))
def test_event_scoped_group_isolates_events(workflow_name: str) -> None:
    """Verify that event-scoped workflows include event_name in the group."""
    workflow = load_workflow(workflow_name)
    concurrency = workflow.get("concurrency", {})
    group = concurrency.get("group", "")

    assert "github.event_name" in group, (
        f"{workflow_name}: concurrency group must include github.event_name "
        "to prevent cross-event cancellation"
    )

    # cancel-in-progress should be static true (event isolation handles safety).
    cancel_in_progress = concurrency.get("cancel-in-progress", "")
    assert cancel_in_progress is True, (
        f"{workflow_name}: cancel-in-progress should be true "
        "(event-scoped groups handle release protection)"
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS_WITH_UNIQUE_GROUPS))
def test_unique_group_protects_releases(workflow_name: str) -> None:
    """Verify workflows using unique groups protect releases via github.sha."""
    workflow = load_workflow(workflow_name)
    concurrency = workflow.get("concurrency", {})
    group = concurrency.get("group", "")

    # Must use github.sha to create unique groups for release commits.
    assert "github.sha" in group, (
        f"{workflow_name}: concurrency group must include github.sha "
        "to create unique groups for release commits"
    )

    # Must check for release commit prefix to conditionally use github.sha.
    assert RELEASE_COMMIT_PREFIX in group, (
        f"{workflow_name}: concurrency group must check for "
        f"'{RELEASE_COMMIT_PREFIX}' to identify release commits"
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS_WITHOUT_CONCURRENCY))
def test_exempt_workflows_no_concurrency(workflow_name: str) -> None:
    """Verify that exempt workflows really carry no concurrency block.

    The exemption list must track reality: a workflow that gains a
    conformant block belongs in the derived `WORKFLOWS_WITH_CONCURRENCY`
    set, where the group-format tests actually inspect it.
    """
    workflow = load_workflow(workflow_name)
    assert workflow is not None, f"{workflow_name} should be a valid workflow"
    assert "concurrency" not in workflow, (
        f"{workflow_name} declares concurrency: remove it from "
        "WORKFLOWS_WITHOUT_CONCURRENCY so the format tests cover it"
    )


TOOL_CACHE_KEY_SUFFIX = (
    "${{ github.job_workflow_sha || hashFiles('repomatic/tool_registry.py') }}"
)
"""The only valid ending for a tool-cache key derived from the registry file."""


def test_tool_cache_keys_rotate_downstream() -> None:
    """Every registry-derived cache key must work from a caller's workspace.

    The cache steps live in reusable workflows called cross-repo, where the
    checkout holds the caller's tree: `hashFiles('repomatic/…')` matches
    nothing there and returns an empty string, collapsing the key to a
    constant. `actions/cache` skips the save on an exact primary-key hit, so
    a constant key freezes every downstream tool cache at its first write.
    `github.job_workflow_sha` is the reusable workflow file's own commit SHA,
    which rotates exactly when the `uses:` pin (and with it the pinned tool
    versions) moves, and is empty on direct upstream runs, where the file
    hash takes over.
    """
    checked = 0
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        workflow = load_workflow(workflow_path.name)
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                if not (isinstance(uses, str) and uses.startswith("actions/cache")):
                    continue
                key = step.get("with", {}).get("key", "")
                if "hashFiles('repomatic/tool_registry.py')" not in key:
                    continue
                checked += 1
                assert key.endswith(TOOL_CACHE_KEY_SUFFIX), (
                    f"{workflow_path.name}:{job_name}: cache key must fall "
                    f"back through github.job_workflow_sha, got: {key}"
                )
    # Guard the sweep against silently checking nothing.
    assert checked >= 9


def test_all_workflows_discovered() -> None:
    """Verify that workflow discovery is working correctly."""
    all_workflows = {p.name for p in WORKFLOWS_DIR.glob("*.yaml")}

    # Verify exempt workflows exist.
    missing_exempt = WORKFLOWS_WITHOUT_CONCURRENCY - all_workflows
    assert not missing_exempt, (
        f"Exempt workflows not found: {missing_exempt}. "
        "Remove them from WORKFLOWS_WITHOUT_CONCURRENCY."
    )

    # Verify unique group workflows exist.
    missing_unique = WORKFLOWS_WITH_UNIQUE_GROUPS - all_workflows
    assert not missing_unique, (
        f"Unique group workflows not found: {missing_unique}. "
        "Remove them from WORKFLOWS_WITH_UNIQUE_GROUPS."
    )

    # Verify unique group workflows are a subset of concurrency workflows.
    not_in_concurrency = WORKFLOWS_WITH_UNIQUE_GROUPS - set(WORKFLOWS_WITH_CONCURRENCY)
    assert not not_in_concurrency, (
        f"Unique group workflows must have concurrency: {not_in_concurrency}"
    )

    # Verify event-scoped workflows exist.
    missing_event_scoped = WORKFLOWS_WITH_EVENT_SCOPED_GROUPS - all_workflows
    assert not missing_event_scoped, (
        f"Event-scoped workflows not found: {missing_event_scoped}. "
        "Remove them from WORKFLOWS_WITH_EVENT_SCOPED_GROUPS."
    )

    # Verify event-scoped workflows are a subset of concurrency workflows.
    not_in_concurrency = WORKFLOWS_WITH_EVENT_SCOPED_GROUPS - set(
        WORKFLOWS_WITH_CONCURRENCY
    )
    assert not not_in_concurrency, (
        f"Event-scoped workflows must have concurrency: {not_in_concurrency}"
    )

    # Verify no overlap between unique groups and event-scoped groups.
    overlap_strategies = (
        WORKFLOWS_WITH_UNIQUE_GROUPS & WORKFLOWS_WITH_EVENT_SCOPED_GROUPS
    )
    assert not overlap_strategies, (
        f"Workflows in both unique and event-scoped categories: {overlap_strategies}"
    )


def test_exemption_list_matches_what_is_on_disk() -> None:
    """Every exempt workflow really lacks a concurrency block, and vice versa.

    The exemption set states an intention; `WORKFLOWS_WITHOUT_CONCURRENCY_BLOCK`
    reads the files. Without this tie, a workflow could sit in the exempt list
    while carrying a block, which is how `debug.yaml` went unchecked: the
    parametrized tests skip it, and the test that should have noticed asserted
    only that its YAML parsed.
    """
    exempt_with_a_block = WORKFLOWS_WITHOUT_CONCURRENCY.intersection(
        WORKFLOWS_WITH_CONCURRENCY_BLOCK
    )
    assert not exempt_with_a_block, (
        f"Exempt from the concurrency requirement but declaring one anyway:"
        f" {sorted(exempt_with_a_block)}. Drop them from"
        " WORKFLOWS_WITHOUT_CONCURRENCY so their block is actually checked."
    )

    blockless_but_required = set(WORKFLOWS_WITHOUT_CONCURRENCY_BLOCK) - (
        WORKFLOWS_WITHOUT_CONCURRENCY
    )
    assert not blockless_but_required, (
        f"Required to declare concurrency but carrying no block:"
        f" {sorted(blockless_but_required)}. Add the block, or document the"
        " exemption in WORKFLOWS_WITHOUT_CONCURRENCY."
    )

    # Verify dynamic discovery found workflows.
    assert WORKFLOWS_WITH_CONCURRENCY, "No workflows discovered for concurrency testing"


# Minutes a job runs for before anything stops it, when it declares no cap of
# its own. `timeout-minutes` defaults to this, and it is also the hard ceiling
# on a GitHub-hosted runner: "Each job in a workflow can run for up to 6 hours
# of execution time. If a job reaches this limit, the job is terminated and
# fails." https://docs.github.com/en/actions/reference/limits
GITHUB_DEFAULT_JOB_TIMEOUT = 360

# Ceiling for any single job's cap, currently held by `compile-binaries`.
# Nothing else has measured within a factor of four of it. A job asking for
# more is either a runaway being blessed as normal, or a workload that wants
# splitting across cells.
MAX_JOB_TIMEOUT = 45

# The only keys GitHub accepts on a job that delegates via `uses:`. Quoted from
# actionlint's own diagnostic, which rejects the file outright: "when a reusable
# workflow is called with `uses`, `timeout-minutes` is not available". A caller
# cannot cap the workflow it calls; the cap belongs on the callee's jobs.
REUSABLE_CALLER_KEYS = frozenset((
    "name",
    "uses",
    "with",
    "secrets",
    "needs",
    "if",
    "permissions",
))


def _iter_jobs() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield `(workflow, job_id, job)` for every job in every workflow."""
    for path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        workflow = load_workflow(path.name)
        for job_id, job in (workflow.get("jobs") or {}).items():
            yield path.name, job_id, job


def test_every_job_caps_its_runtime() -> None:
    """Every job that occupies a runner must declare `timeout-minutes`.

    Without one, a hung step holds its runner for
    `GITHUB_DEFAULT_JOB_TIMEOUT` minutes before the platform reclaims it.
    The macOS and Windows pools are capped per account and shared across every
    repository in it, so one stuck cell there starves all the others for the
    rest of those six hours: the cost of the omission lands on projects that
    have nothing to do with the workflow that hung.

    Callers of a reusable workflow are the one exemption, because GitHub
    rejects the key on them (see `REUSABLE_CALLER_KEYS`). Their runtime
    is still bounded, by the caps on the jobs of the workflow they call, which
    this same sweep covers.
    """
    checked = 0
    uncapped = []
    for workflow, job_id, job in _iter_jobs():
        if "uses" in job:
            continue
        checked += 1
        if job.get("timeout-minutes") is None:
            uncapped.append(f"{workflow}:{job_id}")
    assert not uncapped, (
        f"Jobs occupying a runner with no timeout-minutes: {sorted(uncapped)}."
        f" Each would hold its runner for {GITHUB_DEFAULT_JOB_TIMEOUT} minutes"
        " if it hung. Add a cap sized to the job's measured worst case."
    )
    # Guard the sweep against silently checking nothing.
    assert checked >= 60


def test_job_timeouts_stay_within_bounds() -> None:
    """No cap may be absent-by-another-name: zero, negative, or effectively the
    platform default.

    A cap above `MAX_JOB_TIMEOUT` buys back almost none of the runner
    time the requirement exists to reclaim, so it needs the ceiling raised
    deliberately rather than one job quietly opting out.
    """
    out_of_bounds = [
        f"{workflow}:{job_id}={job['timeout-minutes']}"
        for workflow, job_id, job in _iter_jobs()
        if isinstance(job.get("timeout-minutes"), int)
        and not 1 <= job["timeout-minutes"] <= MAX_JOB_TIMEOUT
    ]
    assert not out_of_bounds, (
        f"Job caps outside 1..{MAX_JOB_TIMEOUT} minutes: {sorted(out_of_bounds)}."
        f" Raise MAX_JOB_TIMEOUT here if a workload genuinely outgrew it."
    )


def test_reusable_callers_carry_no_forbidden_keys() -> None:
    """A `uses:` job may only declare `REUSABLE_CALLER_KEYS`.

    GitHub fails the whole workflow at startup on any other key, so this
    catches a caller that grew a `timeout-minutes` (or a `runs-on`, or
    `steps`) before actionlint runs in CI.
    """
    violations = [
        f"{workflow}:{job_id}:{key}"
        for workflow, job_id, job in _iter_jobs()
        if "uses" in job
        for key in job
        if key not in REUSABLE_CALLER_KEYS
    ]
    assert not violations, (
        f"Keys GitHub rejects on a reusable-workflow caller: {sorted(violations)}."
        f" Only {sorted(REUSABLE_CALLER_KEYS)} are allowed there."
    )

    # Verify no overlap between exempt and concurrency categories.
    overlap = set(WORKFLOWS_WITH_CONCURRENCY) & WORKFLOWS_WITHOUT_CONCURRENCY
    assert not overlap, f"Workflows in both categories: {overlap}"


SITE_DEPLOY_COMPARISON_RE = re.compile(r"site_deploy\s*==\s*'([^']*)'")
"""A deploy-target literal a workflow gate compares `site_deploy` against."""


def _site_deploy_gates() -> list[tuple[str, str, str]]:
    """Every deploy target a workflow gate names, job- and step-level alike.

    :return: One `(workflow, job_id, target)` per comparison found.
    """
    found = []
    for workflow, job_id, job in _iter_jobs():
        conditions = [job.get("if")]
        conditions.extend(step.get("if") for step in job.get("steps") or ())
        for condition in conditions:
            if not isinstance(condition, str):
                continue
            for target in SITE_DEPLOY_COMPARISON_RE.findall(condition):
                found.append((workflow, job_id, target))
    return found


def test_deploy_gates_name_a_declared_target() -> None:
    """Each `site.deploy` target has exactly the jobs it needs, and no more.

    {data}`~repomatic.config.SITE_DEPLOY_TARGETS` promises one deploy job per
    target, so the two halves must agree in both directions and neither is
    checkable from the other side. A gate naming a target the frozenset does
    not carry can never fire, and a workflow whose only deploy job is gated
    that way publishes nothing while running green: `Config.__post_init__`
    rejects the unknown *value*, but nothing reads the *gate*. A target
    declared with no gate behind it is the same failure approached from the
    other end, and is what a repository would hit the day it opted into it.
    """
    gates = _site_deploy_gates()
    assert gates, (
        "No workflow gate compares site_deploy against anything: either the "
        "deploy jobs lost their gates, or this test stopped matching them."
    )
    unknown = sorted(
        f"{workflow}:{job_id} -> {target!r}"
        for workflow, job_id, target in gates
        if target not in SITE_DEPLOY_TARGETS
    )
    assert not unknown, (
        f"Workflow gates naming a target outside SITE_DEPLOY_TARGETS: {unknown}."
        f" Known targets: {sorted(SITE_DEPLOY_TARGETS)}."
    )
    ungated = SITE_DEPLOY_TARGETS - {target for _wf, _job, target in gates}
    assert not ungated, (
        f"Declared site.deploy targets no workflow job runs for: {sorted(ungated)}."
        " A repository selecting one would publish nothing, with every job green."
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS_IGNORING_VERSION_BUMPS))
def test_version_bump_branches_ignored(workflow_name: str) -> None:
    """Heavy PR-triggered workflows must list every VERSION_BUMP_BRANCHES
    member under `pull_request.branches-ignore`.
    """
    workflow = load_workflow(workflow_name)
    pr_trigger = workflow.get("on", {}).get("pull_request", {})
    ignored = set(pr_trigger.get("branches-ignore", ()))
    missing = VERSION_BUMP_BRANCHES - ignored
    assert not missing, (
        f"{workflow_name}: pull_request.branches-ignore is missing "
        f"version-bump branches {sorted(missing)}"
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS_IGNORING_VERSION_BUMPS))
def test_metadata_gate_skips_version_bump_branches(workflow_name: str) -> None:
    """The `metadata` job's `if:` expression must encode the same set of
    branch names as VERSION_BUMP_BRANCHES via a `contains(fromJSON(...))`
    check on `github.head_ref`.

    All PR-triggered workflows that ignore version-bump PRs need this gate
    because `pull_request.branches-ignore` only filters the PR's *base*
    branch (always `main`), so the head-ref check is the only mechanism
    that actually skips these PRs.
    """
    jobs = load_workflow(workflow_name).get("jobs", {})
    if_expr = jobs.get("metadata", {}).get("if", "")
    match = re.search(r"fromJSON\('(\[[^']*\])'\)", if_expr)
    assert match, (
        f"{workflow_name}: metadata.if must use "
        f"`!contains(fromJSON('[...]'), github.head_ref)`. Got: {if_expr!r}"
    )
    gated_branches = set(json.loads(match.group(1)))
    assert gated_branches == set(VERSION_BUMP_BRANCHES), (
        f"{workflow_name}: metadata.if gates {sorted(gated_branches)!r} "
        f"but VERSION_BUMP_BRANCHES is {sorted(VERSION_BUMP_BRANCHES)!r}"
    )


def test_tests_metadata_gate_skips_manual_bumps() -> None:
    """`tests.yaml`'s `metadata` job must short-circuit on every
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES member, but must *not* gate on the
    `[changelog] Post-release bump ` prefix: that push carries the release
    commit, and the post-release-bump commit is the only post-merge test
    point for the release-frozen tree.
    """
    jobs = load_workflow("tests.yaml").get("jobs", {})
    if_expr = jobs.get("metadata", {}).get("if", "")
    pattern = re.compile(
        r"startsWith\(github\.event\.head_commit\.message[^,]*,\s*'([^']+)'\)"
    )
    gated_prefixes = set(pattern.findall(if_expr))
    assert gated_prefixes == set(MANUAL_VERSION_BUMP_COMMIT_PREFIXES), (
        f"tests.yaml: metadata.if gates commit prefixes {sorted(gated_prefixes)!r} "
        f"but MANUAL_VERSION_BUMP_COMMIT_PREFIXES is "
        f"{sorted(MANUAL_VERSION_BUMP_COMMIT_PREFIXES)!r}"
    )
    assert "[changelog] Post-release bump " not in gated_prefixes, (
        "tests.yaml must not gate on '[changelog] Post-release bump ': "
        "the post-release bump push also carries the release commit, "
        "and that's the only post-merge test of release-frozen state."
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS_WITH_METADATA_GATE))
def test_metadata_gate_skips_version_bump_commits(workflow_name: str) -> None:
    """The `metadata` job's `if:` expression must short-circuit on the single
    CHANGELOG_COMMIT_PREFIX clause, so post-merge `push` events with any
    machine-authored version-machinery commit at their head skip the entire
    job graph. The prefix stands in for every VERSION_BUMP_COMMIT_PREFIXES
    member, which `test_changelog_prefix_is_the_machinery_invariant` holds.
    """
    jobs = load_workflow(workflow_name).get("jobs", {})
    if_expr = jobs.get("metadata", {}).get("if", "")
    pattern = re.compile(
        r"startsWith\(github\.event\.head_commit\.message[^,]*,\s*'([^']+)'\)"
    )
    gated_prefixes = set(pattern.findall(if_expr))
    assert gated_prefixes == {CHANGELOG_COMMIT_PREFIX}, (
        f"{workflow_name}: metadata.if must gate on the single "
        f"{CHANGELOG_COMMIT_PREFIX!r} clause, got {sorted(gated_prefixes)!r}"
    )


# Autofix jobs that run without a `needs: metadata` edge, so the central gate
# cannot skip them: each repeats the CHANGELOG_COMMIT_PREFIX clause on its own
# `if:`.
AUTOFIX_METADATA_INDEPENDENT_JOBS = ("fix-typos", "setup-guide", "sync-repomatic")


@pytest.mark.parametrize("job_name", AUTOFIX_METADATA_INDEPENDENT_JOBS)
def test_autofix_independent_jobs_repeat_version_bump_gate(job_name: str) -> None:
    """Metadata-independent autofix jobs carry their own version-bump gate."""
    jobs = load_workflow("autofix.yaml").get("jobs", {})
    job = jobs.get(job_name)
    assert job is not None, f"autofix.yaml no longer defines {job_name}"
    assert "metadata" not in (job.get("needs") or ()), (
        f"{job_name} now depends on metadata: drop it from "
        "AUTOFIX_METADATA_INDEPENDENT_JOBS, the central gate covers it"
    )
    if_expr = job.get("if", "")
    clause = f"!startsWith(github.event.head_commit.message || '', '{CHANGELOG_COMMIT_PREFIX}')"
    assert clause in if_expr, (
        f"autofix.yaml:{job_name}: if must repeat the version-bump gate "
        f"{clause!r}, got {if_expr!r}"
    )


def test_changelog_prefix_is_the_machinery_invariant() -> None:
    """Every machine-authored version-machinery commit shape carries the prefix.

    The single-clause workflow gates above are only sound while every
    version-bump commit message starts with CHANGELOG_COMMIT_PREFIX; this is
    the invariant that lets them stop enumerating message shapes.
    """
    for prefix in VERSION_BUMP_COMMIT_PREFIXES:
        assert prefix.startswith(CHANGELOG_COMMIT_PREFIX), (
            f"{prefix!r} escapes the {CHANGELOG_COMMIT_PREFIX!r} gate"
        )
    assert RELEASE_COMMIT_PATTERN.fullmatch(f"{CHANGELOG_COMMIT_PREFIX}Release v1.2.3")


def test_version_bump_branches_match_changelog_workflow() -> None:
    """VERSION_BUMP_BRANCHES must equal the set of branches actually created
    by `changelog.yaml`'s `peter-evans/create-pull-request` steps.

    `prepare-release` is hard-coded; the `bump-version` job templates
    `${{ matrix.part }}-version-increment` over `matrix.part = [major, minor]`,
    so the full set is `{prepare-release, major-version-increment,
    minor-version-increment}`.
    """
    jobs = load_workflow("changelog.yaml").get("jobs", {})
    created_branches: set[str] = set()
    for job_name in ("prepare-release", "bump-version"):
        job = jobs.get(job_name, {})
        parts = job.get("strategy", {}).get("matrix", {}).get("part") or [None]
        for step in job.get("steps", ()):
            if "repomatic pr-sync" not in (step.get("run") or ""):
                continue
            branch_template = pr_sync_branch(step)
            for part in parts:
                if part is None:
                    created_branches.add(branch_template)
                else:
                    created_branches.add(
                        branch_template.replace("${{ matrix.part }}", part)
                    )
    assert created_branches == set(VERSION_BUMP_BRANCHES), (
        f"changelog.yaml creates {sorted(created_branches)!r} but "
        f"VERSION_BUMP_BRANCHES is {sorted(VERSION_BUMP_BRANCHES)!r}"
    )


def test_release_commit_prefix_in_changelog_workflow() -> None:
    """Verify that changelog.yaml uses the same release commit prefix."""
    workflow = load_workflow("changelog.yaml")

    # Find the prepare-release job's commit message.
    jobs = workflow.get("jobs", {})
    prepare_release = jobs.get("prepare-release", {})
    steps = prepare_release.get("steps", [])

    # Look for the step that creates the freeze commit.
    release_commit_step = None
    for step in steps:
        if step.get("name") == "Create freeze commit":
            release_commit_step = step
            break

    assert release_commit_step is not None, (
        "changelog.yaml must have a 'Create freeze commit' step"
    )

    run_command = release_commit_step.get("run", "")
    assert RELEASE_COMMIT_PREFIX in run_command, (
        f"changelog.yaml release commit must use '{RELEASE_COMMIT_PREFIX}' prefix. "
        f"Found: {run_command}"
    )


def test_version_increments_runs_on_expected_events() -> None:
    """Verify that bump-version job runs on all expected event types.

    The bump-version job depends on the metadata job, which gates execution.
    It runs on push events (to recreate PRs before they conflict), schedule,
    workflow_dispatch, and successful workflow_run events.
    """
    workflow = load_workflow("changelog.yaml")
    jobs = workflow.get("jobs", {})

    # Verify bump-version depends on metadata.
    version_increments = jobs.get("bump-version", {})
    needs = version_increments.get("needs", [])
    assert "metadata" in needs, "bump-version job must depend on metadata"

    # Verify metadata runs on expected events.
    metadata_job = jobs.get("metadata", {})
    condition = metadata_job.get("if", "")
    assert "push" in condition, "metadata job should run on push events"
    assert "schedule" in condition, "metadata job should run on schedule events"
    assert "workflow_dispatch" in condition, (
        "metadata job should run on workflow_dispatch events"
    )
    assert "workflow_run" in condition, "metadata job should run on workflow_run events"


def test_prepare_release_runs_on_workflow_run() -> None:
    """The prepare-release job must run on workflow_run events (the PR-refresh backstop).

    `workflow_run` fires after every "🚀 Build & release" — so after every push to
    `main` — and re-bases the release PR onto the current HEAD. Without it, a push
    that misses changelog.yaml's `paths:` filter (a commit touching only skills,
    a composite action, or a markdown file the filter does not list) leaves the PR
    stale on its prior base. The job may exclude `schedule` (that trigger serves
    bump-version only) but must not exclude `workflow_run`.
    """
    jobs = load_workflow("changelog.yaml").get("jobs", {})
    condition = jobs.get("prepare-release", {}).get("if", "")
    assert condition, "prepare-release job must have an `if:` condition"
    assert "!= 'workflow_run'" not in condition, (
        "prepare-release must run on workflow_run events (the PR-refresh backstop); "
        f"its `if:` must not exclude them. Found: {condition!r}"
    )


def test_workflow_run_references_match_workflow_names() -> None:
    """Every `workflow_run` trigger must watch an existing workflow name.

    GitHub matches the `workflows:` filter against the exact `name:` of the
    watched workflow, and silently drops the trigger when nothing matches. A
    rename is enough to kill it: adding emojis to workflow names in 2026-02
    left `changelog.yaml` watching the old `Build & release` name, so its
    post-release re-trigger never fired again.
    """
    workflow_names = {
        load_workflow(path.name).get("name") for path in WORKFLOWS_DIR.glob("*.yaml")
    }
    for path in WORKFLOWS_DIR.glob("*.yaml"):
        triggers = load_workflow(path.name).get("on", {})
        watch_list = (triggers.get("workflow_run") or {}).get("workflows") or []
        for watched in watch_list:
            assert watched in workflow_names, (
                f"{path.name} watches {watched!r}, which matches no workflow "
                f"name: {sorted(str(name) for name in workflow_names)}"
            )


# changelog.yaml jobs that evaluate `is_version_bump_allowed` and therefore
# need the latest release tag in their checkout. `metadata` computes
# `*_bump_allowed`; `bump-version` runs `close-stale-bump-pr`, which
# re-evaluates the gate. Both resolve the release via `get_latest_tag_version()`,
# so a tag-less checkout makes the decision fall back to "allow" — which
# silently leaks orphan version-bump PRs from the cleanup step.
CHANGELOG_JOBS_REQUIRING_TAGS = ("bump-version", "metadata")


@pytest.mark.parametrize("job_name", CHANGELOG_JOBS_REQUIRING_TAGS)
def test_bump_allowance_jobs_fetch_tags(job_name: str) -> None:
    """Tag-sensitive changelog jobs must check out with `fetch-tags: true`.

    `is_version_bump_allowed` resolves the latest release from Git tags. A job
    that evaluates it without fetching tags falls back to "allow", which
    neutralizes the `close-stale-bump-pr` cleanup and leaves orphan
    version-bump PRs open on `main`.
    """
    jobs = load_workflow("changelog.yaml").get("jobs", {})
    steps = jobs.get(job_name, {}).get("steps", [])

    checkout_steps = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkout_steps, f"{job_name} job must have an actions/checkout step"

    for step in checkout_steps:
        assert step.get("with", {}).get("fetch-tags") is True, (
            f"{job_name} job's checkout must set `fetch-tags: true` so "
            "`is_version_bump_allowed` can resolve the latest release tag. "
            f"Found: {step.get('with', {})}"
        )


def test_post_release_commit_in_changelog_workflow() -> None:
    """Verify that changelog.yaml uses the correct post-release commit message."""
    workflow = load_workflow("changelog.yaml")

    jobs = workflow.get("jobs", {})
    prepare_release = jobs.get("prepare-release", {})
    steps = prepare_release.get("steps", [])

    # Look for the step that creates the post-release commit.
    post_release_step = None
    for step in steps:
        if step.get("name") == "Create unfreeze commit":
            post_release_step = step
            break

    assert post_release_step is not None, (
        "changelog.yaml must have a 'Create unfreeze commit' step"
    )

    run_command = post_release_step.get("run", "")
    assert POST_RELEASE_COMMIT_PREFIX in run_command, (
        f"changelog.yaml post-release commit must use '{POST_RELEASE_COMMIT_PREFIX}'. "
        f"Found: {run_command}"
    )


def test_version_bump_commit_in_changelog_workflow() -> None:
    """Verify that changelog.yaml uses the correct version bump commit message."""
    workflow = load_workflow("changelog.yaml")

    jobs = workflow.get("jobs", {})
    version_increments = jobs.get("bump-version", {})
    steps = version_increments.get("steps", [])

    # The commit message comes from the bump-version template's title, which
    # `test_changelog_prefix_is_the_machinery_invariant` holds to the
    # `[changelog]` prefix; here it is enough that the step is template-driven.
    create_pr_step = None
    for step in steps:
        if "repomatic pr-sync" in (step.get("run") or ""):
            create_pr_step = step
            break

    assert create_pr_step is not None, (
        "changelog.yaml bump-version job must have a pr-sync step"
    )
    assert "--template bump-version" in create_pr_step["run"], (
        "bump-version must render its commit message from the bump-version "
        f"template. Found: {create_pr_step['run']!r}"
    )


PR_SYNC_ENV_KEYS = frozenset(("GH_TOKEN",))
"""Values every `pr-sync` step passes through `env:` rather than its `run:` line.

The token is here because `gh` reads it from the environment and secrets are
not ambient. Everything else the step used to relay (title, body, commit
message, assignee) is now resolved inside `pr-sync` itself: rendered from the
template, or read from ambient variables like `GITHUB_ACTOR`. A step may
declare more (`bump-version` adds `PR_BRANCH`/`PR_PART` to keep `matrix.part`
out of its command line, the dependency steps a diff-table file), never fewer.
"""


def _iter_steps() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield `(workflow, job, step)` for every step in every workflow."""
    for path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        workflow = load_workflow(path.name)
        for job_id, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                yield path.name, job_id, step


def pr_sync_branch(step: dict[str, Any]) -> str:
    """Return the head branch a `pr-sync` step targets.

    Mirrors the CLI's own resolution: an explicit `--branch` wins (resolving
    the shell indirection `bump-version` uses to keep `matrix.part` out of its
    command line), and otherwise the branch defaults to the template name.
    """
    match = re.search(r"--branch \"?(\S+?)\"?(?:\s|$)", step["run"])
    if match:
        branch = match.group(1)
        if branch.startswith("$"):
            env = step.get("env") or {}
            branch = env[branch.lstrip("${").rstrip("}")]
        return branch
    template = re.search(r"--template (\S+)", step["run"])
    assert template, f"neither --branch nor --template in {step['run']!r}"
    return template.group(1)


def test_pull_requests_are_opened_by_the_cli_not_an_action() -> None:
    """No workflow may open a pull request with a third-party action.

    The drift guard for the migration off `peter-evans/create-pull-request`: a
    new job copy-pasting the action from an old example fails here by name.
    """
    found = sorted(
        f"{workflow}::{job}"
        for workflow, job, step in _iter_steps()
        if step.get("uses", "").startswith("peter-evans/create-pull-request@")
    )
    assert found == [], f"still opening PRs with the action: {found}"


def test_pr_sync_steps_pass_everything_through_env() -> None:
    """Every `pr-sync` step names a branch and takes its values from `env:`."""
    steps = [
        (workflow, job, step)
        for workflow, job, step in _iter_steps()
        if "repomatic pr-sync" in (step.get("run") or "")
    ]
    assert steps, "no pr-sync step found: has the migration been reverted?"
    for workflow, job, step in steps:
        where = f"{workflow}::{job}"
        assert pr_sync_branch(step), f"{where} pr-sync step names no branch"
        missing = PR_SYNC_ENV_KEYS - set(step.get("env") or {})
        assert not missing, f"{where} pr-sync env is missing {sorted(missing)}"
        assert "${{" not in step["run"], (
            f"{where} interpolates a template expression into its run block; "
            "pass the value through env: instead"
        )
        # A folded `run: >` scalar silently swallows any orphaned line left
        # behind by an incomplete edit of the step above it, turning YAML
        # residue (`delete-branch: true`) into literal CLI arguments that only
        # Click rejects, at runtime, in CI. `key: value` residue is the shape
        # to catch, and no legitimate pr-sync argument contains a colon.
        assert ":" not in step["run"].replace("pr-sync", ""), (
            f"{where} pr-sync run block carries YAML residue: {step['run']!r}"
        )


def test_pr_sync_templates_declare_their_labels() -> None:
    """Every template a `pr-sync` step renders carries frontmatter labels.

    Labels moved from workflow YAML into template frontmatter; a template
    without them opens an unlabelled pull request, silently, so the gap is
    caught here by template name instead.
    """
    for workflow, job, step in _iter_steps():
        run = step.get("run") or ""
        if "repomatic pr-sync" not in run:
            continue
        template = re.search(r"--template (\S+)", run)
        assert template, f"{workflow}::{job} pr-sync step names no template"
        assert template_labels(template.group(1)), (
            f"template {template.group(1)!r} (used by {workflow}::{job}) "
            "declares no labels in its frontmatter"
        )


def test_pr_sync_steps_are_template_driven() -> None:
    """Every `pr-sync` step names a template rather than relaying rendered text.

    The template is what lets the command resolve title, body, commit message,
    branch, labels and draft state on its own; a step passing `--title`/`--body`
    explicitly would resurrect the `$GITHUB_OUTPUT` relay and its step-output
    size ceiling.
    """
    for workflow, job, step in _iter_steps():
        run = step.get("run") or ""
        if "repomatic pr-sync" not in run:
            continue
        where = f"{workflow}::{job}"
        assert "--template " in run, f"{where} pr-sync step names no template"
        for flag in ("--title", "--body", "--commit-message"):
            assert flag not in run, f"{where} passes {flag} alongside a template"


def test_version_bumps_relock_before_committing() -> None:
    """Every version bump must re-lock before the commit that captures it.

    `bump-my-version` rewrites `pyproject.toml` but not `uv.lock`, so a bump
    committed without a following `uv lock` leaves the tree with the project
    version ahead of its own lock entry. `uv run --frozen` then reinstalls the
    project on the next sync to close that gap, and the release build runs two
    nested syncs: the workflow's, then the one `repomatic run` opens for a
    `needs_venv` tool. The second deletes the console script the first had just
    written, which Windows refuses as a sharing violation and which took the
    Windows binaries out of a release.
    """
    # The `bump` subcommand specifically: `bump-my-version -- show` reads the
    # version without touching the tree, and a commit message may well spell
    # the word "bump" on its own.
    bump_invocation = re.compile(r"bump-my-version\s+--\s+bump\b")

    offenders = []
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        jobs = load_workflow(workflow_path.name).get("jobs", {})
        for job_name, job in jobs.items():
            steps = job.get("steps", []) or []
            for index, step in enumerate(steps):
                run = str(step.get("run", ""))
                if not bump_invocation.search(run):
                    continue
                # Scan forward for the re-lock, stopping at the commit that
                # would otherwise capture the desynchronized tree.
                relocked = False
                for later in steps[index + 1 :]:
                    later_run = str(later.get("run", ""))
                    if re.search(r"\buv\b.*\block\b", later_run):
                        relocked = True
                        break
                    if "git commit" in later_run:
                        break
                if not relocked:
                    offenders.append(
                        f"{workflow_path.name}:{job_name}:{step.get('name', index)}"
                    )

    assert not offenders, (
        "Version bump steps with no `uv lock` before the next commit: "
        f"{offenders}. Add a `Sync uv.lock` step so the committed tree keeps "
        "pyproject.toml and uv.lock on the same version."
    )


def test_broken_links_skips_post_release_commits() -> None:
    """Verify that broken-links job skips post-release version bump commits."""
    workflow = load_workflow("docs.yaml")

    jobs = workflow.get("jobs", {})
    broken_links = jobs.get("check-broken-links", {})
    condition = broken_links.get("if", "")

    # The job should skip post-release commits.
    assert POST_RELEASE_COMMIT_PREFIX in condition, (
        f"check-broken-links job must skip commits containing "
        f"'{POST_RELEASE_COMMIT_PREFIX}'. Found condition: {condition}"
    )


def test_docs_trigger_covers_translated_readmes() -> None:
    """The docs `paths:` filter must fire on a translated readme.

    `check-broken-links` crawls every file `metadata` reports under
    `doc_files`, which globs all Markdown, so a `readme.{lang}.md` is already
    inside the crawl. The trigger is what decides whether the crawl runs at
    all: with `readme.md` as the only readme entry, a push touching just a
    translation skipped the workflow, and those links went unchecked until
    something else happened to touch a listed path. Downstream thin callers
    inherit this list verbatim, so the gap reached every awesome list keeping
    a translation beside its English readme.
    """
    paths = workflow_triggers(load_workflow("docs.yaml"))["push"]["paths"]
    translated = "readme.zh.md"
    assert any(fnmatch(translated, pattern) for pattern in paths), (
        f"docs.yaml push paths {paths} match no translated readme "
        f"({translated!r}), so a translation-only push skips the link crawl."
    )


# --- Action version pinning tests ---


def iter_workflow_actions(workflow: dict):
    """Yield all action references (uses: statements) from a workflow."""
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []):
            if "uses" in step:
                yield job_name, step.get("name", "unnamed"), step["uses"]


def iter_all_actions():
    """Yield all action references from all workflow files.

    Sorted so parametrize IDs are stable across processes (xdist collects in
    every worker and aborts on a mismatch).
    """
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        workflow = load_workflow(workflow_path.name)
        for job_name, step_name, action in iter_workflow_actions(workflow):
            yield workflow_path.name, job_name, step_name, action


# Regex to match action references with pinned versions.
# Accepts: vX.Y.Z, vX.Y, X.Y.Z (some actions don't use v prefix),
# or a full 40-character SHA (commit hash pinning).
# Rejects: vX (major-only).
ACTION_VERSION_PATTERN = re.compile(r"^[^/]+/[^@]+@(v?\d+\.\d+(\.\d+)?|[0-9a-f]{40})$")


@pytest.mark.parametrize(
    ("workflow_name", "job_name", "step_name", "action"),
    list(iter_all_actions()),
    ids=lambda x: x if isinstance(x, str) and "/" in x else None,
)
def test_action_uses_full_semantic_version(
    workflow_name: str, job_name: str, step_name: str, action: str
) -> None:
    """Verify that all actions use full semantic versions (vX.Y.Z), not major-only."""
    # Skip local actions (e.g., ./.github/actions/foo).
    if action.startswith("./"):
        pytest.skip("Local action")

    # Skip Docker actions (e.g., docker://image:tag).
    if action.startswith("docker://"):
        pytest.skip("Docker action")

    # Skip self-references to kdeldycke/repomatic actions.
    # These use @main in development and get rewritten to @vX.Y.Z during release.
    if action.startswith("kdeldycke/repomatic/") and action.endswith("@main"):
        pytest.skip("Self-reference uses @main in development, rewritten on release")

    assert ACTION_VERSION_PATTERN.match(action), (
        f"{workflow_name} ({job_name}/{step_name}): Action '{action}' must use "
        "pinned version (vX.Y.Z or vX.Y), not major-only (vX)"
    )


# --- Runner image convention tests ---


def iter_jobs_with_runners():
    """Yield all jobs with their runner configurations.

    Sorted so parametrize IDs are stable across processes (xdist collects in
    every worker and aborts on a mismatch).
    """
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        workflow = load_workflow(workflow_path.name)
        for job_name, job in workflow.get("jobs", {}).items():
            # Reusable-workflow calls (`uses:`) carry no `runs-on`; skip them.
            if "runs-on" not in job:
                continue
            runs_on = job["runs-on"]
            # Skip matrix-based runners.
            if isinstance(runs_on, str) and not runs_on.startswith("${{"):
                yield workflow_path.name, job_name, runs_on


@pytest.mark.parametrize(
    ("workflow_name", "job_name", "runs_on"),
    list(iter_jobs_with_runners()),
    ids=lambda x: f"{x[0]}:{x[1]}" if isinstance(x, tuple) else None,
)
def test_every_job_runs_on_a_test_axis(
    workflow_name: str, job_name: str, runs_on: str
) -> None:
    """Every literal `runs-on:` names an image the test matrix also covers.

    There is no second set to be an exception to any more: `ubuntu-slim` was the
    last image outside the axes, and it lost the A/B that justified it. Deriving
    the expectation from {data}`~repomatic.lint_repo.KNOWN_RUNNERS` rather than
    from a literal keeps this from failing on a fleet migration instead of on
    the drift it exists to catch.
    """
    assert runs_on in KNOWN_RUNNERS, (
        f"{workflow_name} ({job_name}): unknown runner {runs_on!r}. Every job "
        "runs on a test axis, so widen the runner sets in "
        "repomatic/matrix_axes.py rather than naming a one-off image here."
    )


def test_binary_build_targets_are_the_test_axes() -> None:
    """The Nuitka build fleet is exactly the set of images the suite runs on.

    The companion to {func}`test_every_job_runs_on_a_test_axis`, which can only
    see a literal `runs-on:`. The `compile-binaries` job takes its runner from a
    matrix expression fed by {data}`~repomatic.binary.NUITKA_BUILD_TARGETS`, so
    that test skips it and the build fleet would otherwise be the one place an
    untracked image could sit unnoticed.

    The equality is the invariant, in both directions: a published binary is
    built on an image the suite is validated against, and an image the suite
    covers is one binaries can be built on. Adding a target therefore means
    widening the test axes in {mod}`repomatic.matrix_axes`, never editing this
    fleet alone. `binary.py`'s own docstring states the rule; this holds it.
    """
    build_runners = {target.os for target in NUITKA_BUILD_TARGETS.values()}
    assert build_runners == set(KNOWN_RUNNERS), (
        "Nuitka build targets and the test axes have diverged. Only in "
        f"NUITKA_BUILD_TARGETS: {sorted(build_runners - set(KNOWN_RUNNERS))}; "
        f"only in KNOWN_RUNNERS: {sorted(set(KNOWN_RUNNERS) - build_runners)}. "
        "Widen the runner sets in repomatic/matrix_axes.py so the two agree."
    )


def test_prerelease_python_label_reaches_the_job_name() -> None:
    """The test job titles itself from `python-label`, falling back to the version.

    `repomatic metadata` attaches `python-label` to the cells whose Python is
    unreleased and to no others (see
    {data}`~repomatic.matrix_axes.PRERELEASE_LABEL_SUFFIX`), so the fallback is
    not decoration: on every released cell the key resolves to an empty string,
    and the job would be titled `py` with nothing after it. The metadata half of
    the contract is held by
    `tests/test_metadata.py::test_unstable_python_versions_carry_a_prerelease_label`.
    """
    name = load_workflow("tests.yaml")["jobs"]["tests"]["name"]
    assert "py${{ matrix.python-label || matrix.python-version }}" in name, (
        "The test job name no longer reads the prerelease label, or dropped the "
        f"fallback every released cell needs. Got: {name!r}"
    )


# --- uv provisioning tests ---


def iter_jobs_with_steps():
    """Yield `(workflow_name, job_name, steps)` for every job that has steps.

    Reusable-workflow calls (`uses:` jobs) carry no `steps`, so they are
    skipped: they inherit no toolchain and provision none.
    """
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        workflow = load_workflow(workflow_path.name)
        for job_name, job in workflow.get("jobs", {}).items():
            steps = job.get("steps")
            if steps:
                yield workflow_path.name, job_name, steps


# A `uv`/`uvx` command in a step's run script. The word-boundary match also
# catches the word inside prose or a path (e.g. a `# uv.lock` comment), so it
# is confirmed against command position below to avoid a false positive.
UV_WORD = re.compile(r"\buvx?\b")

# `uv`/`uvx` in command position: at the start of a (logical) line or right
# after a shell operator that begins a new command, followed by whitespace and
# an argument. This ignores `uv` appearing in a comment or a filename.
UV_COMMAND = re.compile(r"(?:^|[\n;&|(]|&&|\|\||`|\$\()[ \t]*(?:uvx|uv)[ \t]")


def _job_invokes_uv(steps: list[dict[str, Any]]) -> bool:
    """Return whether any step's `run` script invokes `uv` or `uvx`."""
    for step in steps:
        run = step.get("run")
        if run and UV_WORD.search(run) and UV_COMMAND.search(run):
            return True
    return False


def _job_provisions_uv(steps: list[dict[str, Any]]) -> bool:
    """Return whether a step provisions uv via `astral-sh/setup-uv`."""
    return any(
        str(step.get("uses", "")).startswith("astral-sh/setup-uv@") for step in steps
    )


# Every (workflow, job) whose steps invoke uv or uvx. Enumerated once so the
# parametrized test both covers each job and fails loudly if the matcher ever
# regresses to matching nothing (guarded by the discovery test below).
UV_INVOKING_JOBS = [
    (workflow_name, job_name)
    for workflow_name, job_name, steps in iter_jobs_with_steps()
    if _job_invokes_uv(steps)
]


def test_uv_invoking_jobs_discovered() -> None:
    """The uv-invocation matcher must find jobs (guards against a dead regex)."""
    assert UV_INVOKING_JOBS, (
        "No uv-invoking jobs discovered. The UV_COMMAND matcher likely "
        "regressed: every workflow runs uv somewhere."
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    UV_INVOKING_JOBS,
    ids=[f"{workflow}:{job}" for workflow, job in UV_INVOKING_JOBS],
)
def test_uv_invoking_job_provisions_setup_uv(workflow_name: str, job_name: str) -> None:
    """Every job that runs `uv`/`uvx` must provision uv via `astral-sh/setup-uv`.

    GitHub Actions jobs share no state: each runs on a fresh runner and
    inherits nothing from the jobs before it, so a job that shells out to uv
    must install uv itself. This cycle's `cancel-runs.yaml` rewrite from a bash
    body to a `repomatic` CLI call dropped the job's `setup-uv` step, and every
    PR-close run failed with `uvx: command not found`. This test locks the
    invariant across all workflows so a uv-invoking job can never ship without
    its own uv provisioning again.
    """
    steps = load_workflow(workflow_name)["jobs"][job_name]["steps"]
    assert _job_provisions_uv(steps), (
        f"{workflow_name} ({job_name}): runs uv/uvx but has no "
        "`astral-sh/setup-uv@` step to provision it. Jobs inherit no toolchain, "
        "so each must provision its own uv."
    )


# --- Condition expression tests ---


def _iter_node_conditions(node: yaml.Node | None) -> Iterator[tuple[int, str]]:
    """Recursively yield `(line, value)` for every `if:` scalar under a node.

    Walks the composed node graph instead of the loaded mapping: only the nodes
    carry the source line a failure has to name.
    """
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if (
                isinstance(key, yaml.ScalarNode)
                and key.value == "if"
                and isinstance(value, yaml.ScalarNode)
            ):
                yield key.start_mark.line + 1, value.value
            yield from _iter_node_conditions(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _iter_node_conditions(item)


def iter_all_conditions() -> Iterator[tuple[str, int, str]]:
    """Yield `(file_path, line, condition)` for every `if:` in the Actions tree.

    Covers the composite actions alongside the workflows: both ship downstream
    and GitHub Actions evaluates their `if:` by the same rule.

    Sorted so parametrize IDs are stable across processes (xdist collects in
    every worker and aborts on a mismatch).
    """
    files = sorted(WORKFLOWS_DIR.glob("*.yaml"))
    files += sorted((REPO_ROOT / ".github" / "actions").glob("*/action.yaml"))
    for path in files:
        root = yaml.compose(path.read_text(encoding="UTF-8"), Loader=yaml.SafeLoader)
        for line, condition in _iter_node_conditions(root):
            yield str(path.relative_to(REPO_ROOT)), line, condition


# Every `if:` condition in the Actions tree. Enumerated once so the parametrized
# test both covers each condition and fails loudly if the walker ever regresses
# to matching nothing (guarded by the discovery test below).
ALL_CONDITIONS = list(iter_all_conditions())

# A condition GitHub Actions evaluates as an expression: the value is one
# `${{ … }}` and nothing else. The `(?!\}\})` guard is what rejects a value
# holding two expressions, which would otherwise pass on its first and last
# three characters alone. Matched with `fullmatch`, so a trailing newline is a
# leftover character and fails, which is the whole point.
SOLE_EXPRESSION = re.compile(r"\$\{\{(?:(?!\}\}).)*\}\}", re.DOTALL)


def test_conditions_discovered() -> None:
    """The condition walker must find conditions (guards against a dead walk)."""
    assert ALL_CONDITIONS, (
        "No `if:` conditions discovered. The _iter_node_conditions walker likely "
        "regressed: every workflow gates something."
    )


@pytest.mark.parametrize(
    ("file_path", "line", "condition"),
    ALL_CONDITIONS,
    ids=[f"{path}:{line}" for path, line, _ in ALL_CONDITIONS],
)
def test_condition_wrapper_spans_the_whole_value(
    file_path: str, line: int, condition: str
) -> None:
    """A condition using `${{ }}` holds nothing outside its braces.

    GitHub Actions evaluates an `if:` value as an expression only when the value
    is *entirely* one `${{ … }}`. Any character outside the braces switches it
    to string interpolation, and the resulting non-empty string is truthy, so
    the step silently stops gating and runs unconditionally.

    A folded block scalar is the trap, because it appends a trailing newline: a
    wrapped expression under `if: >` becomes that expression plus a newline, and
    the character outside the braces is invisible in the diff. Eleven steps of
    the `sync-deps` job in `autofix.yaml` were gated this way, all of them
    always-on until this test was written.

    Write a multi-line condition bare instead, with no wrapper at all: a bare
    `if:` value is evaluated as an expression whatever whitespace trails it. A
    leading `!` (as in `!cancelled()`) rules out the bare *single-line* form,
    since YAML reads it as a tag indicator, so those stay either folded-and-bare
    or single-line-and-wrapped.
    """
    assert "${{" not in condition or SOLE_EXPRESSION.fullmatch(condition), (
        f"{file_path}:{line}: condition {condition!r} has characters outside its "
        "`${{ }}` braces, so GitHub Actions interpolates it into a truthy string "
        "instead of evaluating it, and the gate never holds. Drop the wrapper "
        "and write the expression bare."
    )


# --- Bundled data symlink consistency tests ---

# Path to the bundled data directory.
DATA_DIR = REPO_ROOT / "repomatic" / "data"

# Workflows excluded from the bundled-data-symlink requirement. Two kinds:
# repomatic-internal workflows (patching files only in this repo) and release
# engine lanes (RELEASE_ENGINE_WORKFLOWS) that no code reads via get_data_content,
# so they need no bundled data/ copy. The engine lane that *is* read at runtime
# (_release-engine.yaml, recorded as the release entry's source) stays bundled, so
# it is subtracted out here rather than listed by hand.
_UNBUNDLED_ENGINE_LANES = frozenset(RELEASE_ENGINE_WORKFLOWS) - set(
    WORKFLOW_SOURCES.values()
)
WORKFLOWS_WITHOUT_SYMLINKS = (
    frozenset((
        # repomatic's own release entry; downstreams get a generated release.yaml
        # (assembled from the bundled release.yaml), so the entry isn't bundled.
        "release.yaml",
    ))
    | _UNBUNDLED_ENGINE_LANES
    # Self-maintenance workflows patch this repo's own source and are invisible
    # downstream, so they are deliberately unbundled.
    | SELF_MAINTENANCE_WORKFLOWS
)


def test_all_workflows_have_symlinks_in_data() -> None:
    """Verify that every exportable workflow has a symlink in repomatic/data/."""
    workflows = {
        p.name
        for p in WORKFLOWS_DIR.glob("*.yaml")
        if p.name not in WORKFLOWS_WITHOUT_SYMLINKS
    }
    symlinks = {p.name for p in DATA_DIR.iterdir() if p.is_symlink()}

    missing = workflows - symlinks
    assert not missing, (
        f"Workflows missing symlinks in repomatic/data/: {sorted(missing)}. "
        "Create them with: ln -s ../../.github/workflows/<name> repomatic/data/<name>"
    )


def test_only_workflows_agents_and_actions_are_symlinks_in_data() -> None:
    """Verify only workflow, agent, and composite-action files are symlinks
    in repomatic/data/.

    Scoped to the top level of `repomatic/data/`. Skills live one directory
    down, as a folder per skill, and are covered by
    {func}`test_skill_symlinks_resolve_correctly`.
    """
    workflows = {p.name for p in WORKFLOWS_DIR.glob("*.yaml")}
    agents = {p.name for p in DATA_DIR.iterdir() if p.name.startswith("agent-")}
    actions = {p.name for p in DATA_DIR.iterdir() if p.name.startswith("action-")}
    expected = workflows | agents | actions
    symlinks = {p.name for p in DATA_DIR.iterdir() if p.is_symlink()}

    extra = symlinks - expected
    assert not extra, (
        f"Unexpected symlinks in repomatic/data/: {sorted(extra)}. Only "
        "workflow, agent and composite-action files are symlinked."
    )


def test_workflow_symlinks_resolve_correctly() -> None:
    """Verify that workflow symlinks in repomatic/data/ point to the correct targets."""
    for symlink in sorted(DATA_DIR.iterdir()):
        if not symlink.is_symlink():
            continue
        if symlink.name.startswith(("skill-", "agent-", "action-")):
            continue
        target = symlink.resolve()
        expected = (WORKFLOWS_DIR / symlink.name).resolve()
        assert target == expected, (
            f"Symlink {symlink.name} points to {target}, expected {expected}"
        )


def test_action_symlinks_resolve_correctly() -> None:
    """Verify that composite-action symlinks in repomatic/data/ resolve correctly.

    Uses the registry as the source of truth: every `FileEntry` whose target
    sits under `.github/actions/` must be backed by a symlink at the declared
    `source` path that resolves to the declared target file.
    """
    for component in COMPONENTS:
        for entry in component.files:
            if not entry.target.startswith(".github/actions/"):
                continue
            symlink = DATA_DIR / entry.source
            assert symlink.is_symlink(), (
                f"{symlink} should be a symlink to {entry.target}"
            )
            expected = (REPO_ROOT / entry.target).resolve()
            target = symlink.resolve()
            assert target == expected, (
                f"Symlink {symlink.name} points to {target}, expected {expected}"
            )


def test_skill_symlinks_resolve_correctly() -> None:
    """Verify each bundled skill folder mirrors its `.claude/skills/` original.

    A skill ships as a whole folder, so `repomatic/data/skills/{id}/` must be a
    **real** directory whose every leaf is a symlink to the file of the same
    relative path under `.claude/skills/`. `uv_build` refuses a symlinked
    directory in package data and fails the wheel, while symlinked files are
    dereferenced into it normally.
    """
    skills_dir = REPO_ROOT / ".claude" / "skills"
    bundled_root = DATA_DIR / "skills"
    bundled = sorted(p for p in bundled_root.iterdir() if p.is_dir())
    assert bundled, f"No bundled skill folder found under {bundled_root}"
    for skill_dir in bundled:
        for path in sorted(skill_dir.rglob("*")):
            relative = path.relative_to(bundled_root)
            if path.is_dir():
                assert not path.is_symlink(), (
                    f"{relative} must be a real directory: uv_build cannot "
                    "package a symlinked directory."
                )
                continue
            assert path.is_symlink(), (
                f"{relative} should be a symlink into {skills_dir}"
            )
            expected = (skills_dir / relative).resolve()
            target = path.resolve()
            assert target == expected, (
                f"Symlink {relative} points to {target}, expected {expected}"
            )


def test_agent_symlinks_resolve_correctly() -> None:
    """Verify that agent symlinks in repomatic/data/ point to the correct targets."""
    agents_dir = REPO_ROOT / ".claude" / "agents"
    for symlink in sorted(DATA_DIR.iterdir()):
        if not symlink.is_symlink() or not symlink.name.startswith("agent-"):
            continue
        # agent-grunt-qa.md -> .claude/agents/grunt-qa.md.
        agent_name = symlink.name.removeprefix("agent-")
        expected = (agents_dir / agent_name).resolve()
        target = symlink.resolve()
        assert target == expected, (
            f"Symlink {symlink.name} points to {target}, expected {expected}"
        )


def test_release_build_matrix_outputs_degrade_to_empty_string() -> None:
    """`_release-build.yaml` matrix outputs must emit `''` (not `"null"`) for null.

    GitHub Actions serializes a null value through `toJSON(...)` as the literal
    string `"null"`, which is *truthy* in expressions. A caller that guards its
    `strategy.matrix` with `... || '{"include":[]}'` then never reaches the
    fallback, and `fromJSON("null")` aborts the run with `Unexpected value ''`.
    Every `*_matrix` workflow_call output must therefore degrade to an empty
    string for a null source (the `<value> && toJSON(<value>) || ''` pattern),
    so the caller's fallback fires and the job skips cleanly.
    """
    outputs = load_workflow("_release-build.yaml")["on"]["workflow_call"]["outputs"]
    matrix_outputs = sorted(name for name in outputs if name.endswith("_matrix"))
    assert matrix_outputs, "expected at least one `*_matrix` workflow_call output"
    for name in matrix_outputs:
        value = " ".join(outputs[name]["value"].split())
        assert "|| ''" in value, (
            f"_release-build.yaml output `{name}` must degrade to an empty string "
            "for a null source (the `<value> && toJSON(<value>) || ''` pattern), not "
            'a bare `toJSON(...)` that yields the truthy string "null" and defeats a '
            f"caller's matrix fallback. Got: {value}"
        )


def test_release_lanes_split_jobs() -> None:
    """The build lane owns the package build; the engine lane owns the rest.

    Splitting `build-package` (and the squash guard) into `_release-build.yaml`
    is what lets release.yaml's publish-pypi depend on the wheel alone. The
    engine keeps the binary, tag, and release-finalization jobs and must no
    longer define `build-package`.
    """
    build_jobs = load_workflow("_release-build.yaml")["jobs"]
    engine_jobs = load_workflow("_release-engine.yaml")["jobs"]

    assert "build-package" in build_jobs
    assert "detect-squash-merge" in build_jobs
    assert "metadata" in build_jobs

    assert "build-package" not in engine_jobs
    assert "detect-squash-merge" not in engine_jobs
    # The engine recomputes metadata for its own jobs.
    assert "metadata" in engine_jobs
    assert "compile-binaries" in engine_jobs
    assert "create-release" in engine_jobs

    # No engine job may still depend on the relocated build-package job.
    for name, job in engine_jobs.items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        assert "build-package" not in needs, (
            f"engine job `{name}` still needs the relocated build-package job"
        )


def test_shippability_gate_blocks_the_build_lane() -> None:
    """`build-package` must stay downstream of the `lint-deps` gate.

    The gate is only a gate through this edge. A `lint-deps` job that merely
    reports leaves `build-package` running, which sets `package_built` and
    lets the caller's `publish-pypi` ship a wheel nobody can install: a
    `[tool.uv.sources]` override never reaches the published metadata, so
    there is no other job in the lane that could notice.

    Skipping `build-package` is what closes the whole lane. `package_built`
    resolves false, so `publish-pypi` never fires, and the failed lane skips
    the engine call, taking `create-tag`, `create-release` and
    `publish-release` with it.
    """
    jobs = load_workflow("_release-build.yaml")["jobs"]
    assert "lint-deps" in jobs, "the release lane lost its dependency shippability gate"

    needs = jobs["build-package"].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    assert "lint-deps" in needs, (
        "build-package no longer needs lint-deps, so a release carrying an "
        "unshippable dependency would build and publish anyway"
    )

    # Fatal on a release commit, advisory otherwise: a gate that failed every
    # push would make test-driving a git branch impossible mid-cycle.
    steps = jobs["lint-deps"]["steps"]
    run = next(step["run"] for step in steps if "lint-deps" in step.get("run", ""))
    assert "--fatal" in run and "--no-fatal" in run, (
        "the lint-deps step must pick its severity from release_commits_matrix"
    )
    assert "release_commits_matrix" in run


def test_publish_pypi_does_not_touch_the_github_release() -> None:
    """The publish-pypi lane must not edit the GitHub release (ordering race).

    The GitHub release is created by the engine's `create-release` job, several
    jobs deeper than the caller's fast `publish-pypi` job. A `gh release edit`
    in publish-pypi therefore raced the release's creation, failed with
    "release not found", and (under `continue-on-error`) silently dropped the
    PyPI availability admonition. The admonition is now baked into the notes at
    draft creation, so publish-pypi owns only the PyPI upload and needs no
    `contents` write.
    """
    publish = load_workflow("release.yaml")["jobs"]["publish-pypi"]
    runs = " ".join(step.get("run", "") for step in publish["steps"])
    assert "gh release" not in runs, (
        "publish-pypi must not run any `gh release` command: the GitHub release "
        "is owned by the engine's create-release job, and editing it here races "
        "its creation"
    )
    assert publish["permissions"] == {"id-token": "write"}, (
        "publish-pypi needs only the OIDC token; dropping the release edit drops "
        "its `contents: write` grant too"
    )


def test_create_release_bakes_pypi_admonition_at_creation() -> None:
    """The engine bakes the PyPI availability admonition into the draft notes.

    `create-release` writes the notes while the release is still a draft, so the
    admonition lands there: this avoids both the cross-lane race (above) and the
    immutable-release locks that apply once `publish-release` flips the draft.
    The body falls back to the plain notes for non-PyPI projects, whose
    `release_notes_with_admonition` is empty.
    """
    steps = load_workflow("_release-engine.yaml")["jobs"]["create-release"]["steps"]
    draft = next(s for s in steps if s.get("name") == "Create GitHub release draft")
    notes = " ".join(draft["env"]["RELEASE_NOTES"].split())
    assert "release_notes_with_admonition" in notes
    assert notes.rstrip().endswith(".release_notes }}"), (
        "create-release must fall back to the plain release_notes for non-PyPI "
        "projects (empty admonition body)"
    )


@pytest.mark.parametrize("lane", RELEASE_ENGINE_WORKFLOWS)
def test_release_engine_lane_is_reusable(lane: str) -> None:
    """Each RELEASE_ENGINE_WORKFLOWS lane is a reusable workflow shipped here.

    The generated `release.yaml` calls these lanes by cross-repo `uses:`, so each
    must exist in `.github/workflows/` and declare a `workflow_call` trigger.
    """
    workflow = load_workflow(lane)  # raises if the lane file is missing
    triggers = workflow.get("on", {})
    assert "workflow_call" in triggers, (
        f"{lane} must declare a workflow_call trigger to be a reusable lane"
    )


def test_release_engine_lanes_not_materialized_downstream() -> None:
    """The engine lanes are referenced cross-repo, never deployed as files.

    They must not be downstream `FileEntry` file_ids (the release entry's
    file_id is `release.yaml`), and the entry's recorded source must be one of
    the lanes so {data}`WORKFLOW_SOURCES` and the concept stay consistent.
    """
    materialized = set(RELEASE_ENGINE_WORKFLOWS) & set(ALL_WORKFLOW_FILES)
    assert not materialized, (
        f"engine lanes must not be downstream file_ids: {sorted(materialized)}"
    )
    assert WORKFLOW_SOURCES["release.yaml"] in RELEASE_ENGINE_WORKFLOWS, (
        "the release entry's source must be one of RELEASE_ENGINE_WORKFLOWS"
    )


# ---------------------------------------------------------------------------
# Supply-chain cooldown
# ---------------------------------------------------------------------------

COOLDOWN_EXEMPT_JOBS: dict[str, frozenset[str]] = {
    "tests.yaml": frozenset(("test-package-install",)),
}
"""Jobs allowed to override the workflow-wide cooldown, and why.

`test-package-install` installs the freshly published artifact on purpose, so a
cooldown makes the question it exists to answer unanswerable. See `claude.md`
§ Cooldown on every install for the full exemption roster.
"""


@pytest.mark.parametrize(
    "workflow", sorted(p.name for p in WORKFLOWS_DIR.glob("*.yaml"))
)
def test_workflow_declares_cooldown_env(workflow: str) -> None:
    """Every workflow carries the cooldown `env:` block, verbatim.

    The block is rendered from `[tool.repomatic] minimum-release-age`, so this is
    what keeps the YAML literal tied to its single source of truth: YAML cannot
    read the config itself. A workflow without it silently resolves packages
    published seconds ago.
    """
    content = (WORKFLOWS_DIR / workflow).read_text(encoding="UTF-8")
    assert cooldown_env_block() in content, (
        f"{workflow} is missing the cooldown env block. Re-render it from "
        "repomatic.github.workflow_sync.cooldown_env_block()."
    )


CLI_INVOCATION_RE = re.compile(
    # `\s+` spans the newline plus continuation indent a folded scalar inserts.
    r"uvx\s+--no-progress\s+--from\s+\.\s+repomatic|"
    + LOCAL_CLI_INVOCATION.replace(" ", r"\s+")
)
"""Any way a workflow can invoke this project's CLI from the local checkout."""


@pytest.mark.parametrize(
    "workflow", sorted(p.name for p in WORKFLOWS_DIR.glob("*.yaml"))
)
def test_local_cli_invocation_is_contiguous(workflow: str) -> None:
    """Every local CLI invocation is the exact one-line form the freeze rewrites.

    `freeze_cli_version` is a literal string replacement, so an invocation a
    YAML folded scalar wraps across two lines is invisible to it: the release
    would ship that step still pointing at the local checkout, which downstream
    has no copy of. The same blind spot hides the step from a line-oriented
    grep, which is how one survived the migration off `uvx --from .` and failed
    only once CI reached it.

    Keep the invocation on one line and wrap elsewhere in the command.
    """
    content = (WORKFLOWS_DIR / workflow).read_text(encoding="UTF-8")
    offenders = [
        match.group(0)
        for match in CLI_INVOCATION_RE.finditer(content)
        if "\n" in match.group(0) or match.group(0).startswith("uvx")
    ]
    assert not offenders, (
        f"{workflow}: {len(offenders)} CLI invocation(s) the freeze cannot "
        f"rewrite: {offenders}. Use the contiguous "
        f"`{LOCAL_CLI_INVOCATION}` and wrap the line after it."
    )


# Every (workflow, job) whose steps run the CLI from the local checkout.
LOCAL_CLI_JOBS = [
    (workflow_name, job_name)
    for workflow_name, job_name, steps in iter_jobs_with_steps()
    if any(LOCAL_CLI_INVOCATION in (step.get("run") or "") for step in steps)
]


def test_local_cli_jobs_discovered() -> None:
    """The local-CLI matcher must find jobs (guards against a dead constant)."""
    assert LOCAL_CLI_JOBS, (
        f"No job runs `{LOCAL_CLI_INVOCATION}`. The constant likely drifted "
        "from what the workflows spell: every lane dogfoods the CLI somewhere."
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    LOCAL_CLI_JOBS,
    ids=[f"{workflow}:{job}" for workflow, job in LOCAL_CLI_JOBS],
)
def test_local_cli_job_checks_out_repo(workflow_name: str, job_name: str) -> None:
    """Every job running the CLI from `uv.lock` must check out the repository.

    `uv run --frozen` resolves the project from the working directory, so
    without a checkout there is no `pyproject.toml`, no `uv.lock`, and no
    console script: the step dies on `Failed to spawn: repomatic`.

    The `publish-release` job shipped without one on the assumption that the
    freeze had rewritten its invocation to the PyPI-resolved `uvx` form. It had,
    on the release commit — but a rebase-merged release PR pushes the freeze and
    unfreeze commits together, and Actions runs the workflow at the push head,
    so the unfrozen form is what executes upstream. See
    `repomatic/prepare_release.py` for the contract.
    """
    steps = load_workflow(workflow_name)["jobs"][job_name]["steps"]
    assert any(
        str(step.get("uses", "")).startswith("actions/checkout@") for step in steps
    ), (
        f"{workflow_name} ({job_name}): runs `{LOCAL_CLI_INVOCATION}` but never "
        "checks out the repository, so uv has no project to resolve the CLI "
        "from. Add an `actions/checkout@` step."
    )


# Every (workflow, job) whose steps both check out the repository and download a
# run artifact, in either order.
CHECKOUT_AND_DOWNLOAD_JOBS = [
    (workflow_name, job_name)
    for workflow_name, job_name, steps in iter_jobs_with_steps()
    if any(str(step.get("uses", "")).startswith("actions/checkout@") for step in steps)
    and any(
        str(step.get("uses", "")).startswith("actions/download-artifact@")
        for step in steps
    )
]


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    CHECKOUT_AND_DOWNLOAD_JOBS,
    ids=[f"{workflow}:{job}" for workflow, job in CHECKOUT_AND_DOWNLOAD_JOBS],
)
def test_checkout_precedes_artifact_download(workflow_name: str, job_name: str) -> None:
    """A job downloading run artifacts must check out *before* it downloads.

    `actions/checkout` deletes the contents of its target directory whenever
    that directory holds no `.git` of its own, which is exactly the state a
    fresh workspace is in after `actions/download-artifact` has written to it:

        Deleting the contents of '/home/runner/work/{repo}/{repo}'

    So the download silently loses everything it fetched, and the job carries on
    against an empty tree. `publish-release` acquired a checkout below its
    binary download this way, which would have left the next release an
    unpublished draft with no binary attached.

    Reversing the two is the whole fix: a checkout into a pristine workspace has
    nothing to delete, and the download then writes into the checked-out tree.
    """
    steps = load_workflow(workflow_name)["jobs"][job_name]["steps"]
    kinds = [
        "checkout"
        if str(step.get("uses", "")).startswith("actions/checkout@")
        else "download"
        for step in steps
        if str(step.get("uses", "")).startswith((
            "actions/checkout@",
            "actions/download-artifact@",
        ))
    ]
    assert kinds.index("checkout") < kinds.index("download"), (
        f"{workflow_name} ({job_name}): downloads a run artifact before "
        "`actions/checkout`, which then deletes it. Move the checkout above "
        "the download."
    )


@pytest.mark.parametrize(
    "workflow", sorted(p.name for p in WORKFLOWS_DIR.glob("*.yaml"))
)
def test_cooldown_override_is_declared(workflow: str) -> None:
    """No job weakens the cooldown without being listed as a known exemption.

    A job-level `env:` silently outranks the workflow-level block, so an
    undeclared override is the one way the cooldown can disappear while every
    other check still passes.
    """
    allowed = COOLDOWN_EXEMPT_JOBS.get(workflow, frozenset())
    overriding = {
        job_id
        for job_id, job in (load_workflow(workflow).get("jobs") or {}).items()
        if isinstance(job, dict) and "UV_EXCLUDE_NEWER" in (job.get("env") or {})
    }
    assert overriding <= allowed, (
        f"{workflow}: job(s) {sorted(overriding - allowed)} override "
        "UV_EXCLUDE_NEWER without being declared in COOLDOWN_EXEMPT_JOBS. "
        "Add the job with a comment naming what breaks without the override."
    )


# ---------------------------------------------------------------------------
# Pinned uv toolchain
# ---------------------------------------------------------------------------

SETUP_UV_STEP_RE = re.compile(r"uses:[^\S\n]*astral-sh/setup-uv@[0-9a-f]{40}")

UV_PINNED_FILES = sorted([
    *WORKFLOWS_DIR.glob("*.yaml"),
    *(REPO_ROOT / ".github" / "actions").glob("*/*.yaml"),
])


@pytest.mark.parametrize("path", UV_PINNED_FILES, ids=lambda p: p.name)
def test_every_setup_uv_step_pins_a_version(path: Path) -> None:
    """Every `setup-uv` step declares the uv version it installs.

    Two reasons this must hold everywhere rather than mostly. Without the input,
    `setup-uv` installs the newest uv satisfying `required-version`, so the tool
    enforcing every cooldown arrives without one. And
    {data}`~repomatic.version_sync._SETUP_UV_VERSION_RE` scans lazily from the
    `uses:` line to the next `version:`, so a step missing the input would silently
    borrow the following step's and `sync-workflow-pins` would rewrite the wrong
    line.
    """
    content = path.read_text(encoding="UTF-8")
    steps = len(SETUP_UV_STEP_RE.findall(content))
    if not steps:
        pytest.skip("no setup-uv steps")
    pins = len([lit for lit in find_workflow_literals(content) if lit.package == "uv"])
    assert pins == steps, (
        f"{path.name}: {steps} setup-uv step(s) but {pins} version pin(s). "
        'Every setup-uv step needs `with: version: "X.Y.Z"`.'
    )


def test_setup_uv_pins_agree() -> None:
    """All `setup-uv` pins name one uv version.

    `sync-workflow-pins` resolves a single version per package, so a split pin
    would leave one of the two silently un-bumped.
    """
    versions = {
        lit.version
        for path in UV_PINNED_FILES
        for lit in find_workflow_literals(path.read_text(encoding="UTF-8"))
        if lit.package == "uv"
    }
    assert len(versions) == 1, f"setup-uv pins disagree: {sorted(versions)}"


def test_report_step_outputs_are_consumed() -> None:
    """A report emitted to a step output must be read back somewhere.

    `--output-format github-actions` is the hand-off shape: the command
    spills a report to a file and names it as a `<key>_file` step output,
    for a later step to feed into a pull request body
    (`REPOMATIC_DIFF_TABLE_FILE`, `--template-arg-file`). `format-images`
    lost its summary table to exactly that gap in `7.11.0`: the step
    emitting `markdown_file` survived the collapse of the PR-publishing
    steps, but its consumer did not, and a body-less template rendered as
    if the report never existed. An output nobody references is that
    orphan shape by construction, whatever the key.
    """
    for path in WORKFLOWS_DIR.glob("*.yaml"):
        text = path.read_text(encoding="UTF-8")
        doc = yaml.safe_load(text)
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                if "--output-format github-actions" not in run:
                    continue
                step_id = step.get("id")
                assert step_id, (
                    f"{path.name} job {job_name!r} emits step outputs "
                    "without a step id, so nothing can consume them"
                )
                assert f"steps.{step_id}.outputs" in text, (
                    f"{path.name} job {job_name!r} step {step_id!r} emits "
                    "step outputs no other step or job reads back: the "
                    "report it hands over is orphaned"
                )


# ---------------------------------------------------------------------------
# Extra release assets
# ---------------------------------------------------------------------------

RELEASE_ASSET_ARTIFACT_PREFIX = "release-asset-"
"""Prefix the engine's `extra-assets` job matches run artifacts on."""


def _declared_release_assets() -> list[str]:
    """Filenames `[tool.repomatic] release-assets` declares for this repository."""
    pyproject = tomlrt.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="UTF-8"))
    assets = pyproject.get("tool", {}).get("repomatic", {}).get("release-assets", [])
    return [str(name) for name in assets]


def test_plugin_archive_is_a_declared_release_asset() -> None:
    """The packed plugin's filename is one of the declared release assets.

    `pack-plugin` writes {data}`~repomatic.plugin.ARCHIVE_NAME` by default and
    `release.yaml` passes no `--output`, so renaming the constant on its own
    would leave `release-assets` and the upload `path:` naming a file no job
    produces. The test below then holds that filename equal to the run-artifact
    name and the `needs:` edge, which is what makes the handover checkable end
    to end from a Python constant TOML and YAML cannot read.
    """
    assert ARCHIVE_NAME in _declared_release_assets()


def test_declared_release_assets_are_space_free() -> None:
    """No declared filename can contain a space.

    The engine passes the list through a space-separated environment variable and
    word-splits it in bash, so a space silently turns one filename into two and
    the completeness check passes vacuously.
    """
    for name in _declared_release_assets():
        assert " " not in name, f"release-assets entry {name!r} contains a space"


@pytest.mark.parametrize("asset", _declared_release_assets())
def test_declared_release_asset_has_a_producing_job(asset: str) -> None:
    """Every declared asset is produced and handed over by a job in release.yaml.

    Three spellings have to agree for the handover to work, in three files no
    single tool reads at once: the filename in `[tool.repomatic] release-assets`,
    the `release-asset-<filename>` run-artifact name, and the `needs:` edge that
    makes the artifact exist before the engine looks for it. Any one of them
    drifting fails the release at `extra-assets`, or worse, publishes an immutable
    release without the asset.
    """
    jobs = load_workflow("release.yaml")["jobs"]

    artifact = f"{RELEASE_ASSET_ARTIFACT_PREFIX}{asset}"
    producers = [
        job_id
        for job_id, config in jobs.items()
        for step in config.get("steps") or ()
        if (step.get("with") or {}).get("name") == artifact
    ]
    assert producers, (
        f"No job in release.yaml uploads a {artifact!r} artifact for the "
        f"{asset!r} entry in [tool.repomatic] release-assets."
    )

    release_needs = jobs["release"].get("needs") or []
    if isinstance(release_needs, str):
        release_needs = [release_needs]
    for producer in producers:
        assert producer in release_needs, (
            f"release.yaml's `release` job must list {producer!r} in `needs:`, or "
            f"the engine's extra-assets job can run before {artifact!r} exists."
        )
