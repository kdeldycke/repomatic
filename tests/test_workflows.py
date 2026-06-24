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
from pathlib import Path
from typing import Any

import pytest
import yaml

from repomatic.binary import (
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    VERSION_BUMP_BRANCHES,
    VERSION_BUMP_COMMIT_PREFIXES,
)
from repomatic.registry import (
    ALL_WORKFLOW_FILES,
    RELEASE_ENGINE_WORKFLOWS,
    WORKFLOW_SOURCES,
)

# Self-referential URL base for this repository.
SELF_REF_URL_BASE = "https://raw.githubusercontent.com/kdeldycke/repomatic"

# Branch used in self-referential URLs during development.
SELF_REF_BRANCH = "main"

# Common prefix for all changelog-related commits.
CHANGELOG_COMMIT_PREFIX = "[changelog]"

# Commit message prefix that identifies release commits. These commits are protected
# from cancellation to ensure proper tagging, PyPI publishing, and GitHub releases.
RELEASE_COMMIT_PREFIX = f"{CHANGELOG_COMMIT_PREFIX} Release"

# Commit message prefix for post-release version bump.
POST_RELEASE_COMMIT_PREFIX = f"{CHANGELOG_COMMIT_PREFIX} Post-release bump"

# Root of the repository.
REPO_ROOT = Path(__file__).parent.parent

# Path to the workflows directory.
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Workflows that are exempt from concurrency requirements.
WORKFLOWS_WITHOUT_CONCURRENCY = frozenset((
    "autolock.yaml",  # Scheduled only, no concurrent execution possible.
    "cancel-runs.yaml",  # Fires on PR close, must always run to completion.
    "debug.yaml",  # Debug-only workflow, not for production use.
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
    # workflow_run events from "Build & release" cancel push-triggered runs
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
# for tests.yaml's narrower gating.
WORKFLOWS_WITH_METADATA_GATE = frozenset((
    "labels.yaml",
    "lint.yaml",
))

# Workflows that must use conditional cancel-in-progress (excludes unique
# group and event-scoped workflows).
WORKFLOWS_WITH_CONDITIONAL_CANCEL = tuple(
    sorted(
        name
        for name in WORKFLOWS_WITH_CONCURRENCY
        if name not in WORKFLOWS_WITH_UNIQUE_GROUPS
        and name not in WORKFLOWS_WITH_EVENT_SCOPED_GROUPS
    )
)


def load_workflow(workflow_name: str) -> dict[str, Any]:
    """Load and parse a workflow YAML file."""
    workflow_path = WORKFLOWS_DIR / workflow_name
    with workflow_path.open(encoding="utf-8") as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


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


@pytest.mark.parametrize("workflow_name", WORKFLOWS_WITH_UNIQUE_GROUPS)
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


@pytest.mark.parametrize("workflow_name", WORKFLOWS_WITHOUT_CONCURRENCY)
def test_exempt_workflows_no_concurrency(workflow_name: str) -> None:
    """Verify that exempt workflows do not have concurrency configured."""
    workflow = load_workflow(workflow_name)
    # These workflows are exempt and may or may not have concurrency.
    # This test documents the exemption.
    assert workflow is not None, f"{workflow_name} should be a valid workflow"


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

    # Verify dynamic discovery found workflows.
    assert WORKFLOWS_WITH_CONCURRENCY, "No workflows discovered for concurrency testing"

    # Verify no overlap between exempt and concurrency categories.
    overlap = set(WORKFLOWS_WITH_CONCURRENCY) & WORKFLOWS_WITHOUT_CONCURRENCY
    assert not overlap, f"Workflows in both categories: {overlap}"


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
    """The `metadata` job's `if:` expression must short-circuit on every
    VERSION_BUMP_COMMIT_PREFIXES member via a `startsWith(github.event.head_commit.message, ...)`
    clause, so post-merge `push` events with bot-authored version-bump
    commit messages skip the entire job graph.
    """
    jobs = load_workflow(workflow_name).get("jobs", {})
    if_expr = jobs.get("metadata", {}).get("if", "")
    pattern = re.compile(
        r"startsWith\(github\.event\.head_commit\.message[^,]*,\s*'([^']+)'\)"
    )
    gated_prefixes = set(pattern.findall(if_expr))
    assert gated_prefixes == set(VERSION_BUMP_COMMIT_PREFIXES), (
        f"{workflow_name}: metadata.if gates commit prefixes "
        f"{sorted(gated_prefixes)!r} but VERSION_BUMP_COMMIT_PREFIXES is "
        f"{sorted(VERSION_BUMP_COMMIT_PREFIXES)!r}"
    )


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
            uses = step.get("uses", "")
            if not uses.startswith("peter-evans/create-pull-request@"):
                continue
            branch_template = step.get("with", {}).get("branch", "")
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

    # Look for the create-pull-request step which contains the commit message.
    create_pr_step = None
    for step in steps:
        if step.get("uses", "").startswith("peter-evans/create-pull-request"):
            create_pr_step = step
            break

    assert create_pr_step is not None, (
        "changelog.yaml bump-version job must have a create-pull-request step"
    )

    commit_message = create_pr_step.get("with", {}).get("commit-message", "")
    # The commit message is now sourced from the pr-metadata step output.
    assert "pr-metadata.outputs.commit_message" in commit_message, (
        "bump-version commit message must reference pr-metadata output. "
        f"Found: {commit_message}"
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


# --- Action version pinning tests ---


def iter_workflow_actions(workflow: dict):
    """Yield all action references (uses: statements) from a workflow."""
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []):
            if "uses" in step:
                yield job_name, step.get("name", "unnamed"), step["uses"]


def iter_all_actions():
    """Yield all action references from all workflow files."""
    for workflow_path in WORKFLOWS_DIR.glob("*.yaml"):
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


# Jobs that require ubuntu-24.04 instead of ubuntu-slim.
# Each entry documents the reason for the exception.
UBUNTU_2404_EXCEPTIONS = {
    # Format: (workflow_name, job_name): "reason"
    (
        "autofix.yaml",
        "format-markdown",
    ): "ubuntu-slim lacks shfmt, required by mdformat-shfmt",
    ("renovate.yaml", "renovate"): "renovatebot/github-action requires Docker",
}


def iter_jobs_with_runners():
    """Yield all jobs with their runner configurations."""
    for workflow_path in WORKFLOWS_DIR.glob("*.yaml"):
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
def test_runner_uses_ubuntu_slim_by_default(
    workflow_name: str, job_name: str, runs_on: str
) -> None:
    """Verify that jobs use ubuntu-slim unless there's a documented exception."""
    if runs_on == "ubuntu-slim":
        return  # Correct default.

    if runs_on == "ubuntu-24.04":
        exception_key = (workflow_name, job_name)
        assert exception_key in UBUNTU_2404_EXCEPTIONS, (
            f"{workflow_name} ({job_name}): Uses 'ubuntu-24.04' but is not in "
            "UBUNTU_2404_EXCEPTIONS. Either use 'ubuntu-slim' or document the "
            "exception with a reason."
        )
        return

    # Other runners (macos, windows) are allowed for cross-platform testing.
    if any(
        platform in runs_on for platform in ("macos", "windows", "ubuntu-24.04-arm")
    ):
        return

    pytest.fail(f"{workflow_name} ({job_name}): Unknown runner '{runs_on}'")


# --- Bundled data symlink consistency tests ---

# Path to the bundled data directory.
DATA_DIR = REPO_ROOT / "repomatic" / "data"

# Path to the composite actions directory.
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

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
        # Patches repomatic/tool_runner.py, which only exists here.
        "update-checksums.yaml",
    ))
    | _UNBUNDLED_ENGINE_LANES
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


def test_only_workflows_skills_agents_and_actions_are_symlinks_in_data() -> None:
    """Verify only workflow, skill, agent, and composite-action files are symlinks
    in repomatic/data/.
    """
    workflows = {p.name for p in WORKFLOWS_DIR.glob("*.yaml")}
    skills = {p.name for p in DATA_DIR.iterdir() if p.name.startswith("skill-")}
    agents = {p.name for p in DATA_DIR.iterdir() if p.name.startswith("agent-")}
    actions = {p.name for p in DATA_DIR.iterdir() if p.name.startswith("action-")}
    expected = workflows | skills | agents | actions
    symlinks = {p.name for p in DATA_DIR.iterdir() if p.is_symlink()}

    extra = symlinks - expected
    assert not extra, (
        f"Unexpected symlinks in repomatic/data/: {sorted(extra)}. "
        "Only workflow, skill, agent, and composite-action files should be symlinked."
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
    from repomatic.registry import COMPONENTS

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
    """Verify that skill symlinks in repomatic/data/ point to the correct targets."""
    skills_dir = REPO_ROOT / ".claude" / "skills"
    for symlink in sorted(DATA_DIR.iterdir()):
        if not symlink.is_symlink() or not symlink.name.startswith("skill-"):
            continue
        # skill-repomatic-changelog.md -> .claude/skills/repomatic-changelog/SKILL.md.
        skill_name = symlink.name.removeprefix("skill-").removesuffix(".md")
        expected = (skills_dir / skill_name / "SKILL.md").resolve()
        target = symlink.resolve()
        assert target == expected, (
            f"Symlink {symlink.name} points to {target}, expected {expected}"
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
