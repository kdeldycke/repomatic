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

"""Registry of the cooldown-respecting dependency updaters, and their driver.

The five `sync-*` dependency bumpers (`sync-dep-sources`, `sync-uv-lock`,
`sync-tool-versions`, `sync-action-pins`, `sync-workflow-pins`) share a shape:
discover the latest eligible upstream version, gated by the
`[tool.repomatic] minimum-release-age` cooldown (or uv's `exclude-newer` for
the lock), then rewrite the pin. This module turns that shape into data: one
{class}`SyncOperation` per bumper, in {data}`SYNC_OPERATIONS`.

The registry is the single source of truth consumed three ways: the thin
`sync-*` commands and the aggregate `sync-deps` command in {mod}`repomatic.cli`,
and the consolidated CI job emitted by {mod}`repomatic.github.workflow_sync`.

```{rubric} Resolve then apply
```

Each operation splits into a read phase and a write phase so `sync-deps` can run
the slow, network-bound discovery for every operation concurrently, then write
serially:

- {attr}`SyncOperation.resolve` performs the network discovery and computes the
  new file contents in memory, returning a {class}`SyncPlan`. It does not touch
  the repository, so the resolves are safe to run in parallel.
- {attr}`SyncOperation.apply` writes the planned contents. Three of the five
  operations rewrite `.github/workflows/*.yaml` (action pins, workflow literals,
  and the actionlint matcher URL all live there), so applies must run serially.

`sync-uv-lock` and `sync-dep-sources` are the documented exceptions: their
discovery *is* a mutation (`uv lock` rewrites `uv.lock`), so their
{attr}`SyncOperation.resolve` writes during the parallel phase and their
{attr}`SyncOperation.apply` is a no-op. Their shared write domain (`uv.lock`,
`pyproject.toml`) is disjoint from every other operation's, and the two are
serialized against each other through {data}`_UV_PROJECT_MUTEX`. A `--dry-run`
resolve snapshots and restores the mutated files so the preview leaves no
trace.

The datasource adapters, version selection, and pure string rewriters live in
{mod}`repomatic.version_sync` and {mod}`repomatic.uv`; this module composes them
with the file I/O and checksum recompute. Terminal and PR-body rendering stay in
{mod}`repomatic.cli`, fed from the {class}`SyncPlan`.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path

import click
from click_extra import OperationTrail, echo, get_tool_config, resolve_jobs, run_jobs

from . import tool_registry
from .checksums import update_registry_checksums
from .dep_report import (
    BYPASS_COLUMNS,
    EXCLUDE_NEWER_HELD_BACK_NOTE,
    HELD_BACK_COLUMNS,
    BypassForecast,
    HeldBackPackage,
    build_comparison_urls,
    build_held_back,
    fetch_release_notes,
    format_bypass_section,
    format_diff_table,
    format_exclude_newer_note,
    format_held_back_table,
    format_release_notes,
    format_released,
    format_upload_date,
    pypi_name_urls,
)
from .dep_sources import (
    ReleaseSwap,
    apply_release_swaps,
    find_ready_swaps,
    format_swap_section,
    tracked_git_overrides,
)
from .github.actions import emit_report
from .github.pr_body import template_docs_url
from .github.releases import fetch_github_release_notes, resolve_tag_to_sha
from .init_project import init_config, is_source_repo
from .npm import NPM_PACKAGE_URL
from .prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from .pypi import PYPI_PACKAGE_URL, get_source_url
from .registry import (
    BUNDLED_VERBATIM_TARGETS,
    DEFAULT_REPO,
    GITHUB_YAML_PATTERNS,
    UPSTREAM_PACKAGE,
    UPSTREAM_REPO_SLUGS,
)
from .tool_registry import TOOL_REGISTRY, ToolBackend
from .uv import (
    LockFile,
    compute_bypass_forecasts,
    compute_held_back_packages,
    diff_lock_versions,
    parse_lock_versions,
    sync_uv_lock,
    upsert_exclude_newer_packages,
    uv_lock_command,
)
from .version_sync import (
    MIN_AGE_HELD_BACK_NOTE,
    SETUP_UV_PACKAGE,
    SETUP_UV_SLUG,
    apply_action_pins,
    apply_workflow_literals,
    find_action_pins,
    find_upstream_ref_versions,
    find_workflow_literals,
    format_cooldown_note,
    github_candidates,
    is_newer,
    npm_candidates,
    parse_min_age,
    pin_inside_cooldown,
    pypi_candidates,
    select_held_back,
    select_latest,
    set_tool_version,
    set_with_package_version,
    setup_uv_verified_versions,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import timedelta
    from typing import Any

    from click_extra import Context

    from .config import Config
    from .version_sync import Candidate

DEPENDENCY_LABEL = "🔗 dependencies"
"""GitHub label applied to every dependency-update PR.

Shared by all five bumpers so a single label filters the whole family. Workflow
YAML cannot import Python, so `autofix.yaml` repeats this string literally, and
the labeller's own rule tables ({data}`repomatic.labels.DEFAULT_CONTENT_RULES`
and {data}`~repomatic.labels.DEFAULT_FILE_RULES`) key their dependency rules on
the same spelling. `tests/test_sync_ops.py` asserts both copies match this
constant, and `tests/test_labels.py` that it names a label `labels.toml`
actually defines: applying an unknown label fails the `gh` call outright, so a
rename in the registry has to reach all of them at once.
"""

_UV_PROJECT_MUTEX = threading.Lock()
"""Serializes the two resolves that mutate the uv project files.

`sync-uv-lock` and `sync-dep-sources` both rewrite `pyproject.toml` and run
`uv lock` during their resolve phase, while `sync-deps` fans the resolves out
concurrently. Interleaving two lock runs (or a snapshot/restore pair) on the
same project corrupts both, so the two resolves take this mutex for their
whole duration.
"""


def _workflow_and_action_files() -> list[Path]:
    """Collect workflow and composite-action YAML files under `.github/`."""
    files: list[Path] = []
    for pattern in GITHUB_YAML_PATTERNS:
        files.extend(Path().glob(pattern))
    return sorted(set(files))


def _pinnable_files() -> dict[Path, str]:
    """Read the `.github/` workflow and action files whose pins are bumpable.

    Downstream, drops the files `repomatic init` deploys verbatim
    ({data}`~repomatic.registry.BUNDLED_VERBATIM_TARGETS`): their `uses:` pins are
    owned by the bundled template, so a bump here is undone by the next
    `sync-repomatic` and the two pull requests ping-pong. This is the per-file
    counterpart to the per-slug {data}`~repomatic.registry.UPSTREAM_REPO_SLUGS`
    skip in :func:`_resolve_action_pins`.

    In the source repo the exclusion lifts: there the bundled template is a symlink
    to the in-tree file, so init rewrites nothing and the pin is a normal
    source-of-truth ref that upstream keeps bumping (see
    {func}`~repomatic.init_project.is_source_repo`).
    """
    in_source_repo = is_source_repo(Path.cwd())
    return {
        path: path.read_text(encoding="UTF-8")
        for path in _workflow_and_action_files()
        if in_source_repo or path.as_posix() not in BUNDLED_VERBATIM_TARGETS
    }


def _sync_actionlint_matcher_url(version: str) -> None:
    """Align the actionlint matcher URL in `lint.yaml` with the registry version.

    The matcher URL embeds the actionlint version as a release tag; it is a
    reference derived from the registry, kept in lockstep with the
    `sync-tool-versions` bump.
    """
    lint_path = Path(".github/workflows/lint.yaml")
    if not lint_path.is_file():
        return
    content = lint_path.read_text(encoding="UTF-8")
    updated = re.sub(
        r"(actionlint/refs/tags/v)[0-9][0-9.]*(/)",
        rf"\g<1>{version}\g<2>",
        content,
    )
    if updated != content:
        lint_path.write_text(updated, encoding="UTF-8")


@dataclass
class ResolveContext:
    """Inputs shared by every {attr}`SyncOperation.resolve`.

    Each operation reads the subset it needs. The cooldown is derived from
    *config* (`minimum-release-age` for the version-sync trio, `exclude-newer`
    from the lock for `sync-uv-lock`).
    """

    config: Config
    """The resolved `[tool.repomatic]` configuration."""

    today: date
    """Reference date for the cooldown computation, fixed once per run."""

    release_notes: bool = False
    """Fetch upstream release notes and append them to the report."""

    held_back: bool = True
    """Report newer releases withheld only by the cooldown."""

    dry_run: bool = False
    """Plan without persisting: restore any files the resolve had to mutate."""

    lockfile: Path = field(default_factory=lambda: Path("uv.lock"))
    """Path to `uv.lock` for `sync-uv-lock`."""


@dataclass
class ToolVersionExtras:
    """`sync-tool-versions` write extras, applied after the source rewrite."""

    binary_overrides: dict[str, str] = field(default_factory=dict)
    """Binary tool name to new version, for the checksum recompute."""

    actionlint_version: str | None = None
    """New actionlint version, for the matcher-URL realignment."""

    checksums_path: Path | None = None
    """The `tool_registry.py` path the checksum recompute rewrites."""


@dataclass
class UvProjectExtras:
    """Extras of the uv-project pair (`sync-uv-lock`, `sync-dep-sources`).

    The pair shares one write domain (`uv.lock`, `pyproject.toml`) and resolves
    under {data}`_UV_PROJECT_MUTEX`. Each resolve already wrote those files, so
    these fields only inform the terminal and PR-body rendering.
    """

    exclude_newer: str = ""
    """The `exclude-newer` cutoff from the lock, or empty."""

    reverted: bool = False
    """Whether a cosmetic-only re-lock was discarded."""

    pins_synced: bool = False
    """Whether the `[tool.uv]` policy pins were refreshed from the template."""

    pruned_bypasses: list[BypassForecast] = field(default_factory=list)
    """Expired `exclude-newer-package` entries removed from `pyproject.toml`,
    snapshot with the version and expiry each freeze had."""

    frozen_bypasses: list[str] = field(default_factory=list)
    """`exclude-newer-package` entries rewritten into freeze cutoffs."""

    bypass_forecasts: list[BypassForecast] = field(default_factory=list)
    """Active cooldown-bypass freezes with their expiry forecasts."""

    source_swaps: list[ReleaseSwap] = field(default_factory=list)
    """Git-tracked dependencies swapped to their released versions."""


@dataclass
class SyncPlan:
    """The resolved, not-yet-written outcome of one operation's read phase.

    Carries everything {attr}`SyncOperation.apply` needs to write the changes
    and everything {mod}`repomatic.cli` needs to render the terminal table and
    the markdown PR body, so the write and the rendering never re-resolve.
    """

    operation: str
    """The operation name (`sync-uv-lock`, …)."""

    subject: str
    """Header for the first table column (`Package`, `Tool`, `Action`)."""

    heading: str
    """Noun after `## 🆙 ` in the diff table (`Updated tools`, …)."""

    changes: list[tuple[str, str, str]] = field(default_factory=list)
    """Applied `(name, old, new)` triples, in the order the report renders."""

    dates: dict[str, str] = field(default_factory=dict)
    """Name to release/upload date (`YYYY-MM-DD` or ISO 8601) for the table."""

    released_overrides: dict[str, str] = field(default_factory=dict)
    """Name to literal markdown replacing its "Released" table cell.

    Marks rows whose version was decided outside the cooldown-checked release
    listing (the upstream toolkit's lockstep-aligned pin), so the table shows
    the exemption instead of a blank cell.
    """

    name_urls: dict[str, str] = field(default_factory=dict)
    """Name to the URL its table cell links to (PyPI, GitHub, npm)."""

    comparison_urls: dict[str, str] = field(default_factory=dict)
    """Name to a compare URL linked on the change cell."""

    held_back: list[HeldBackPackage] = field(default_factory=list)
    """Newer releases withheld only by the cooldown."""

    held_back_name_urls: dict[str, str] = field(default_factory=dict)
    """Name to URL for the held-back section."""

    held_back_note: str = MIN_AGE_HELD_BACK_NOTE
    """Intro paragraph for the held-back section (cooldown wording)."""

    notes_section: str = ""
    """Pre-rendered release-notes markdown, or empty."""

    cooldown_note: str = ""
    """Pre-rendered cooldown-cutoff sentence shown above the diff table."""

    cutoff: date | None = None
    """Effective `minimum-release-age` cutoff, for the terminal report."""

    reference_date: date | None = None
    """Reference date for the table's relative "Released" hints (the run date)."""

    file_writes: dict[Path, str] = field(default_factory=dict)
    """Path to its new full text, as computed at resolve time.

    Written verbatim by {attr}`SyncOperation.apply` only when {attr}`rebase`
    is unset; otherwise it records which files the resolve touched (and what
    it computed) while the apply replays the rewrite on current disk state.
    """

    self_pin_exemptions: list[str] = field(default_factory=list)
    """Workflow files that gained a missing self-pin cooldown exemption.

    Names only the files whose *sole* edit was the splice, since a file that
    also moved a version is already reported through {attr}`changes`. Kept as a
    separate list for the same reason {attr}`UvProjectExtras.frozen_bypasses`
    is: the rewrite has no `(name, old, new)` triple to render, yet it still
    produced a hunk the report has to explain. Without it a splice-only run
    reads as "nothing to update" and the write is dropped, which is what let an
    exemption-less downstream pin sit broken indefinitely: the backfill only
    ever landed on a run that happened to move the version too.
    """

    rebase: Callable[[str], tuple[str, list[tuple[str, str, str]]]] | None = None
    """Replay this operation's rewriter against a file's current text.

    Set by {func}`_plan_file_rewrites`. Applies run serially after every
    resolve finished, and the two `.github/` pin updaters routinely plan
    rewrites of the same workflow files from the same pre-apply snapshot:
    writing {attr}`file_writes` verbatim would silently revert whichever
    sibling applied first. The closure re-runs the pure rewriter on whatever
    is on disk at apply time instead.
    """

    tool_versions: ToolVersionExtras = field(default_factory=ToolVersionExtras)
    """`sync-tool-versions` write extras (checksum recompute, matcher URL)."""

    uv_project: UvProjectExtras = field(default_factory=UvProjectExtras)
    """`sync-uv-lock` and `sync-dep-sources` rendering extras."""

    @property
    def has_changes(self) -> bool:
        """Whether the operation found anything to update.

        Cooldown-bypass edits count: a run that only prunes or freezes
        `exclude-newer-package` entries still rewrites `pyproject.toml` and
        must produce a report explaining that hunk. A run that only splices a
        missing self-pin exemption into a workflow counts for the same reason.
        """
        return bool(
            self.changes
            or self.self_pin_exemptions
            or self.uv_project.pruned_bypasses
            or self.uv_project.frozen_bypasses
        )

    def note_cooldown(self, age_label: str, min_age: timedelta, today: date) -> None:
        """Record the cooldown cutoff and its rendered diff-table note.

        No-op fields (a `None` cutoff, an empty note) when the cooldown is
        disabled (`0 days` or unparsable).
        """
        self.cutoff = today - min_age if min_age else None
        self.cooldown_note = (
            format_cooldown_note(age_label, self.cutoff)
            if self.cutoff is not None
            else ""
        )


def _track_held_back(
    plan: SyncPlan,
    rc: ResolveContext,
    name: str,
    url: str,
    candidates: list[Candidate],
    latest: Candidate | None,
    pinned: str,
    min_age: timedelta,
) -> None:
    """Record on *plan* the release withheld from *name* only by the cooldown.

    Held-back covers every scanned item, even ones not bumped this run, so the
    section shows the full cooldown pipeline. The probe compares against the
    version this run settles on: *latest* when it beats *pinned*, else
    *pinned*. Skipped when the caller opted out (`rc.held_back` off).
    """
    if not rc.held_back:
        return
    final_version = (
        latest.version
        if latest is not None and is_newer(latest.version, pinned)
        else pinned
    )
    withheld = select_held_back(candidates, final_version, min_age, rc.today)
    if withheld is not None:
        plan.held_back.append(
            build_held_back(
                name,
                final_version,
                withheld.version,
                withheld.date,
                min_age,
                rc.today,
            )
        )
        plan.held_back_name_urls[name] = url


def _finish_uv_plan(plan: SyncPlan, rc: ResolveContext, lockfile: Path) -> None:
    """Attach the held-back probe, release notes and compare URLs to a uv plan.

    The shared tail of the uv-project pair. Both operations end by asking uv the
    same two follow-up questions about the lock they just wrote, so the wiring
    (and the reason each step is gated) lives here once.

    :param plan: The plan being resolved, mutated in place.
    :param rc: The resolve inputs, supplying the opt-in flags.
    :param lockfile: The `uv.lock` the operation just rewrote.
    """
    # The probe runs a second uv resolution, so skip it with nothing to report:
    # the caller already gates `held_back` on a consumer being present (terminal
    # table or file output). Bypass edits count as changes: a prune may leave
    # newer releases cooldown-held, and this is the report that explains them.
    if rc.held_back and plan.has_changes:
        plan.held_back = compute_held_back_packages(lockfile)
        plan.held_back_name_urls = pypi_name_urls([
            (pkg.name, "", "") for pkg in plan.held_back
        ])

    notes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    if rc.release_notes and plan.changes:
        notes = fetch_release_notes(plan.changes)
        plan.notes_section = format_release_notes(notes)
    plan.comparison_urls = build_comparison_urls(plan.changes, notes)


def _resolve_uv_lock(rc: ResolveContext) -> SyncPlan:
    """Re-lock dependencies and roll cooldown overrides forward.

    Unlike the version-sync trio, the discovery here is the mutation:
    `uv lock --upgrade` rewrites `uv.lock` in place. The new contents are left
    on disk (the write domain is shared only with `sync-dep-sources`, guarded
    by {data}`_UV_PROJECT_MUTEX`), so {func}`_apply_uv_lock` is a no-op. A
    `--dry-run` resolve snapshots `uv.lock` and `pyproject.toml` up front and
    restores them in a `finally`.
    """
    plan = SyncPlan(
        operation="sync-uv-lock",
        subject="Package",
        heading="Updated packages",
        held_back_note=EXCLUDE_NEWER_HELD_BACK_NOTE,
    )
    plan.reference_date = rc.today
    lockfile = rc.lockfile
    pyproject_path = lockfile.parent / "pyproject.toml"
    with _UV_PROJECT_MUTEX:
        lock_before = lockfile.read_bytes() if lockfile.exists() else None
        pyproject_before = (
            pyproject_path.read_bytes() if pyproject_path.exists() else None
        )

        def restore() -> None:
            if lock_before is not None:
                lockfile.write_bytes(lock_before)
            if pyproject_before is not None:
                pyproject_path.write_bytes(pyproject_before)

        try:
            # Sync repomatic's canonical [tool.uv] policy pins from the bundled
            # template before re-locking, so a bumped exclude-newer feeds the
            # resolution and a bumped required-version rides in the same change.
            if pyproject_path.exists():
                merged = init_config("uv", pyproject_path)
                if merged is not None:
                    pyproject_path.write_text(merged, encoding="UTF-8")
                    plan.uv_project.pins_synced = True

            result = sync_uv_lock(lockfile)
            plan.changes = result.changes
            plan.dates = result.upload_times
            plan.uv_project.exclude_newer = result.exclude_newer
            plan.uv_project.reverted = result.reverted
            plan.cooldown_note = format_exclude_newer_note(result.exclude_newer)
            plan.name_urls = pypi_name_urls(result.changes)
            plan.uv_project.pruned_bypasses = result.pruned_bypasses
            plan.uv_project.frozen_bypasses = result.frozen_bypasses
            plan.uv_project.bypass_forecasts = result.bypass_forecasts

            _finish_uv_plan(plan, rc, lockfile)
        except BaseException:
            # A failure mid-resolve must not leave a half-mutated uv project
            # (synced pins with a stale lock) for the CI job to commit:
            # restore both snapshots and re-raise, matching
            # `_resolve_dep_sources`.
            restore()
            raise
        finally:
            if rc.dry_run:
                restore()
    return plan


def _apply_uv_lock(plan: SyncPlan) -> None:
    """No-op: {func}`_resolve_uv_lock` already wrote `uv.lock`."""


def _resolve_dep_sources(rc: ResolveContext) -> SyncPlan:
    """Swap git-tracked dependencies whose awaited release shipped.

    Like `sync-uv-lock`, the discovery is the mutation: the `pyproject.toml`
    rewrite and the `uv lock` run happen here, guarded by
    {data}`_UV_PROJECT_MUTEX`, and {func}`_apply_dep_sources` is a no-op.
    Every failure path restores the snapshots taken up front: a swap is
    all-or-nothing, so a resolution conflict or a lock landing on the wrong
    version degrades to "nothing to report" instead of a half-applied swap.
    """
    plan = SyncPlan(
        operation="sync-dep-sources",
        subject="Package",
        heading="Updated packages",
        held_back_note=EXCLUDE_NEWER_HELD_BACK_NOTE,
    )
    plan.reference_date = rc.today
    lockfile = rc.lockfile
    pyproject_path = lockfile.parent / "pyproject.toml"
    if not pyproject_path.exists():
        return plan

    # The probe also runs under the mutex: the sibling `sync-uv-lock` resolve
    # rewrites `pyproject.toml` (pin sync, prune, freeze) and reading a
    # half-written file would corrupt the swap decision.
    with _UV_PROJECT_MUTEX:
        swaps = find_ready_swaps(pyproject_path)
        if not swaps:
            return plan

        lock_before = lockfile.read_bytes() if lockfile.exists() else None
        pyproject_before = pyproject_path.read_bytes()
        before = parse_lock_versions(lockfile)

        def restore() -> None:
            pyproject_path.write_bytes(pyproject_before)
            if lock_before is not None:
                lockfile.write_bytes(lock_before)

        try:
            apply_release_swaps(pyproject_path, swaps)
            upsert_exclude_newer_packages(
                pyproject_path,
                {swap.name: swap.freeze_cutoff for swap in swaps},
            )
            subprocess.run(
                uv_lock_command(pyproject_path), check=True, cwd=lockfile.parent
            )
        except subprocess.CalledProcessError:
            # A conflicting constraint elsewhere in the tree: not this run's
            # call to untangle. Leave the project as found and report nothing.
            restore()
            logging.warning("Dependency source swap failed to resolve; skipped.")
            return plan
        except BaseException:
            restore()
            raise

        post = LockFile.load(lockfile)
        after = post.versions
        missed = [swap.name for swap in swaps if after.get(swap.name) != swap.release]
        if missed:
            restore()
            logging.warning(
                f"Swap landed on unexpected versions for {', '.join(missed)};"
                " restored the project untouched."
            )
            return plan

        plan.uv_project.source_swaps = swaps
        plan.changes = diff_lock_versions(before, after)
        plan.dates = post.upload_times
        plan.uv_project.exclude_newer = post.exclude_newer
        plan.cooldown_note = format_exclude_newer_note(plan.uv_project.exclude_newer)
        plan.name_urls = pypi_name_urls(plan.changes)
        # The fresh freezes are this run's news: they render as 📌 rows.
        plan.uv_project.frozen_bypasses = [swap.name for swap in swaps]
        plan.uv_project.bypass_forecasts = compute_bypass_forecasts(
            pyproject_path, lockfile
        )

        _finish_uv_plan(plan, rc, lockfile)

        if rc.dry_run:
            restore()
    return plan


def _apply_dep_sources(plan: SyncPlan) -> None:
    """No-op: {func}`_resolve_dep_sources` already wrote the swap."""


def _pinned_with_packages() -> list[tuple[str, str]]:
    """Every `package==version` pin declared in a tool's `with_packages`.

    These ride along in a tool's `uvx` environment (plugin sets, and the engine a
    plugin shells out to) and never surface as a `repomatic run` tool of their
    own, so nothing else in the bump path looks at them.

    When two tools pin the same package the *lowest* version wins the lookup, so
    a bump is proposed as long as any one of them trails the latest release;
    {func}`~repomatic.version_sync.set_with_package_version` then rewrites every
    occurrence, converging them.

    :return: Sorted `(package, pinned_version)` pairs.
    """
    pins: dict[str, str] = {}
    for spec in TOOL_REGISTRY.values():
        for entry in spec.with_packages:
            package, separator, version = entry.partition("==")
            if not separator:
                continue
            if package not in pins or is_newer(pins[package], version):
                pins[package] = version
    return sorted(pins.items())


def _resolve_tool_versions(rc: ResolveContext) -> SyncPlan:
    """Bump every `repomatic run` tool to its latest eligible release."""
    min_age = parse_min_age(rc.config.minimum_release_age)
    today = rc.today
    plan = SyncPlan(
        operation="sync-tool-versions", subject="Tool", heading="Updated tools"
    )
    plan.reference_date = today
    registry_path = Path(tool_registry.__file__)
    content = registry_path.read_text(encoding="UTF-8")

    changes: list[tuple[str, str, str]] = []
    overrides: dict[str, str] = {}
    for name, spec in sorted(TOOL_REGISTRY.items()):
        backend = spec.backend
        if backend is ToolBackend.BINARY:
            if not spec.source_url:
                continue
            candidates = github_candidates(spec.source_url, spec.tag_pattern)
        elif backend is ToolBackend.NPM:
            candidates = npm_candidates(spec.package or spec.name)
        else:
            candidates = pypi_candidates(spec.pypi_name)
        latest = select_latest(candidates, min_age, today)
        _track_held_back(
            plan,
            rc,
            name,
            spec.datasource_url,
            candidates,
            latest,
            spec.version,
            min_age,
        )
        if latest is None or not is_newer(latest.version, spec.version):
            continue
        content = set_tool_version(content, name, latest.version)
        changes.append((name, spec.version, latest.version))
        plan.dates[name] = latest.date
        overrides[name] = latest.version

    # Second pass over the packages pinned *alongside* a tool. Without it they
    # drift silently: a plugin pin ages out, or a second copy of a tool the main
    # registry has already moved on from goes stale (mdformat once carried its
    # own `ruff==` pin, formatting the same Markdown code blocks as the top-level
    # ruff, at a different version).
    package_urls: dict[str, str] = {}
    package_notes: list[tuple[str, str, str, str, str | None]] = []
    for package, pinned in _pinned_with_packages():
        pypi_url = PYPI_PACKAGE_URL.format(package=package)
        candidates = pypi_candidates(package)
        latest = select_latest(candidates, min_age, today)
        _track_held_back(
            plan, rc, package, pypi_url, candidates, latest, pinned, min_age
        )
        if latest is None or not is_newer(latest.version, pinned):
            continue
        content = set_with_package_version(content, package, latest.version)
        changes.append((package, pinned, latest.version))
        plan.dates[package] = latest.date
        # Prefer the GitHub repository, matching ToolSpec.datasource_url's order,
        # and fall back to the PyPI project page for a package declaring none.
        # A resolved repo also earns the package release notes: no `tag_pattern`
        # to consult here, so the default `vX.Y.Z` scheme applies (it also passes
        # a bare `X.Y.Z` tag through untouched).
        source = get_source_url(package)
        package_urls[package] = source or pypi_url
        if source:
            package_notes.append((package, source, pinned, latest.version, None))

    plan.changes = changes
    plan.note_cooldown(rc.config.minimum_release_age, min_age, today)
    if not changes:
        return plan

    plan.file_writes = {registry_path: content}
    plan.tool_versions.checksums_path = registry_path
    plan.tool_versions.binary_overrides = {
        name: version
        for name, version in overrides.items()
        if TOOL_REGISTRY[name].binary is not None
    }
    plan.tool_versions.actionlint_version = overrides.get("actionlint")
    # `changes` now mixes registry tools with bare `with_packages` pins, so every
    # TOOL_REGISTRY lookup below has to tolerate a name that is not a tool.
    plan.name_urls = {
        name: TOOL_REGISTRY[name].datasource_url
        for name, _old, _new in changes
        if name in TOOL_REGISTRY
        and (TOOL_REGISTRY[name].source_url or TOOL_REGISTRY[name].npm is not None)
    } | package_urls

    if rc.release_notes:
        tool_notes = [
            (name, src, old, new, TOOL_REGISTRY[name].tag_pattern)
            for name, old, new in changes
            if name in TOOL_REGISTRY
            and (src := TOOL_REGISTRY[name].source_url)
            and "github.com" in src
        ]
        plan.notes_section = format_release_notes(
            fetch_github_release_notes(tool_notes + package_notes)
        )
    return plan


def _apply_tool_versions(plan: SyncPlan) -> None:
    """Rewrite `tool_registry.py`, recompute checksums, realign the matcher URL."""
    if not plan.has_changes or plan.tool_versions.checksums_path is None:
        return
    path = plan.tool_versions.checksums_path
    path.write_text(plan.file_writes[path], encoding="UTF-8")
    # Recompute checksums and VERSIONS stamps for bumped binary tools in the same
    # pass, downloading at the new versions (the in-memory registry still holds
    # the pre-bump versions because the source was edited, not reimported).
    if plan.tool_versions.binary_overrides:
        update_registry_checksums(
            path, version_overrides=plan.tool_versions.binary_overrides
        )
    if plan.tool_versions.actionlint_version:
        _sync_actionlint_matcher_url(plan.tool_versions.actionlint_version)


def _widest_changes(
    changes: Iterable[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Collapse a name updated from several versions onto its widest range.

    Files can pin the same action or package at different versions, but one
    run converges them all onto a single resolved target, so a name's
    `(name, old, new)` triples differ only by *old*. Reporting each range
    separately duplicates table rows, and the name-keyed mappings derived
    from the changes (compare URLs, release-notes items) would silently keep
    one arbitrary range. Keeping the lowest parseable *old* per name yields a
    single row whose compare URL and release notes subsume every narrower
    range.

    :param changes: `(name, old, new)` triples, possibly repeating a name.
    :return: One triple per name, sorted, spanning from that name's lowest
        *old* (`v` prefixes are ignored for the comparison).
    """
    widest: dict[str, tuple[str, str]] = {}
    for name, old, new in changes:
        kept = widest.get(name)
        if kept is None or is_newer(kept[0].removeprefix("v"), old.removeprefix("v")):
            widest[name] = (old, new)
    return [(name, old, new) for name, (old, new) in sorted(widest.items())]


def _plan_file_rewrites(
    plan: SyncPlan,
    rc: ResolveContext,
    file_data: dict[Path, str],
    rewriter: Callable[[str, Any], tuple[str, list[tuple[str, str, str]]]],
    resolved: Any,
    min_age: timedelta,
) -> None:
    """Apply a pin rewriter across every scanned file and record the outcome.

    The write-planning half of the two `.github/` pin updaters: run the pure
    rewriter over each file's text, keep the ones it changed for
    {func}`_apply_file_writes`, and collapse the per-file change lists into one
    row per name.

    :param plan: The plan being resolved, mutated in place.
    :param rc: The resolve inputs, supplying the cooldown label.
    :param file_data: Each scanned file's path and current text.
    :param rewriter: A pure `(text, resolved) -> (new_text, changes)` function.
    :param resolved: The rewriter's target versions, keyed however it expects.
    :param min_age: The cooldown window, for the diff-table note.
    """
    changes: list[tuple[str, str, str]] = []
    for path, text in file_data.items():
        new_content, file_changes = rewriter(text, resolved)
        # Gate on the text, not on `file_changes`. A rewriter may edit a file
        # without having a version move to report: `apply_workflow_literals`
        # splices a missing self-pin cooldown exemption that way. Gating on the
        # change list dropped those writes on the floor, so the backfill its
        # docstring promises only ever landed on a run that also bumped a
        # version, and a repo already pinned at the newest release never got it.
        if new_content == text:
            continue
        plan.file_writes[path] = new_content
        changes.extend(file_changes)
        if not file_changes:
            plan.self_pin_exemptions.append(path.name)
    plan.rebase = lambda text: rewriter(text, resolved)
    plan.changes = _widest_changes(changes)
    plan.note_cooldown(rc.config.minimum_release_age, min_age, rc.today)


def _resolve_action_pins(rc: ResolveContext) -> SyncPlan:
    """Bump SHA-pinned GitHub Actions across `.github/` to their latest release.

    An action pinned at more than one version also has its stragglers
    converged onto the highest pin, even when no newer release clears the
    cooldown, so every file agrees with the version the report claims.
    """
    min_age = parse_min_age(rc.config.minimum_release_age)
    today = rc.today
    plan = SyncPlan(
        operation="sync-action-pins", subject="Action", heading="Updated actions"
    )
    plan.reference_date = today
    file_data = _pinnable_files()

    # Highest currently-pinned version per slug, skipping repomatic-lineage refs
    # (those thin-caller pins are managed by `repomatic init`). The winning
    # pin's `(sha, ref)` is kept so slugs pinned at more than one version can
    # converge their stragglers onto it without a network round-trip, even
    # when no newer release clears the cooldown.
    current: dict[str, str] = {}
    current_pins: dict[str, tuple[str, str]] = {}
    mixed: set[str] = set()
    for text in file_data.values():
        for pin in find_action_pins(text):
            if pin.slug in UPSTREAM_REPO_SLUGS:
                continue
            version = pin.ref.removeprefix("v")
            if pin.slug not in current:
                current[pin.slug] = version
                current_pins[pin.slug] = (pin.sha, pin.ref)
                continue
            if version != current[pin.slug]:
                mixed.add(pin.slug)
                if is_newer(version, current[pin.slug]):
                    current[pin.slug] = version
                    current_pins[pin.slug] = (pin.sha, pin.ref)

    resolved: dict[str, tuple[str, str]] = {}
    for slug, current_version in sorted(current.items()):
        repo_url = f"https://github.com/{slug}"
        candidates = github_candidates(repo_url)
        latest = select_latest(candidates, min_age, today)
        _track_held_back(
            plan, rc, slug, repo_url, candidates, latest, current_version, min_age
        )
        if latest is None or not is_newer(latest.version, current_version):
            # No release beats the highest pin, but stragglers pinned at older
            # versions still converge onto it: otherwise the repo-wide maximum
            # masks them until a strictly newer release clears the cooldown,
            # and the held-back table reports a "Locked" version that some
            # files do not actually pin. The winning on-disk pin already
            # carries the SHA, so no tag resolution is needed.
            if slug in mixed:
                resolved[slug] = current_pins[slug]
                pinned_date = next(
                    (c.date for c in candidates if c.version == current_version),
                    "",
                )
                if pinned_date:
                    plan.dates[slug] = pinned_date
            continue
        new_sha = resolve_tag_to_sha(repo_url, latest.ref)
        if not new_sha:
            logging.warning(f"Could not resolve {slug}@{latest.ref} to a commit SHA.")
            continue
        resolved[slug] = (new_sha, latest.ref)
        plan.dates[slug] = latest.date

    _plan_file_rewrites(plan, rc, file_data, apply_action_pins, resolved, min_age)
    plan.name_urls = {
        slug: f"https://github.com/{slug}" for slug, _old, _new in plan.changes
    }
    plan.comparison_urls = {
        slug: f"https://github.com/{slug}/compare/{old}...{new}"
        for slug, old, new in plan.changes
    }

    if rc.release_notes:
        # Actions tag as `vX.Y.Z`, so no per-tool extraction pattern is needed.
        notes_items: list[tuple[str, str, str, str, str | None]] = [
            (
                slug,
                f"https://github.com/{slug}",
                old.removeprefix("v"),
                new.removeprefix("v"),
                None,
            )
            for slug, old, new in plan.changes
        ]
        plan.notes_section = format_release_notes(
            fetch_github_release_notes(notes_items)
        )
    return plan


def _gate_uv_on_checksums(
    candidates: list[Candidate],
    pinned: str,
    file_data: Mapping[Path, str],
    min_age: timedelta,
    today: date,
) -> list[Candidate]:
    """Drop uv releases the pinned `setup-uv` cannot checksum-verify.

    The uv pin is the one literal whose adoptable range is decided by a
    *second* pin: `setup-uv` verifies a download only against the checksum
    table its own release bundles, and silently skips verification for anything
    else (see {func}`~repomatic.version_sync.setup_uv_verified_versions`).
    Walking uv forward past that table therefore trades a hash for nothing,
    which is the opposite of what pinning it was for.

    So the ceiling moves when the action pin moves, not when uv publishes.
    Nothing is downgraded: the caller only adopts a candidate newer than what
    is pinned, so a repository already past its ceiling simply stays put until
    `sync-action-pins` lands a `setup-uv` that covers it.

    :param candidates: Every uv release, as offered by PyPI.
    :param pinned: The uv version currently written in the workflows.
    :param file_data: The scanned files, read for their `setup-uv` pins.
    :param min_age: The stabilization window, to name what the gate withheld.
    :param today: Reference date for the cooldown computation.
    :return: The candidates carrying a checksum, or all of them when no table
        could be read.
    """
    shas = {
        pin.sha
        for text in file_data.values()
        for pin in find_action_pins(text)
        if pin.slug == SETUP_UV_SLUG
    }
    verified = setup_uv_verified_versions(shas)
    if verified is None:
        return candidates

    # Audit the pin already on disk, not just the one about to replace it: the
    # same reason pin_inside_cooldown exists, for a condition that likewise
    # entered the tree without passing this gate (hand-edited, or written
    # before the gate existed). Nothing here can fix it, since the repair is a
    # sync-action-pins bump, so it is reported rather than acted on.
    if pinned not in verified:
        logging.warning(
            f"uv {pinned} is pinned but carries no checksum in the pinned"
            f" {SETUP_UV_SLUG}, so every job installs it unverified. Bump the"
            " action pin to restore verification."
        )

    gated = [candidate for candidate in candidates if candidate.version in verified]
    ungated_latest = select_latest(candidates, min_age, today)
    gated_latest = select_latest(gated, min_age, today)
    if ungated_latest is not None and (
        gated_latest is None or is_newer(ungated_latest.version, gated_latest.version)
    ):
        logging.info(
            f"uv {ungated_latest.version} cleared the cooldown but carries no"
            f" checksum in the pinned {SETUP_UV_SLUG}: holding the pin until"
            " the action pin catches up."
        )
    return gated


def _resolve_workflow_pins(rc: ResolveContext) -> SyncPlan:
    """Bump npm and PyPI version literals embedded in workflow YAML.

    The upstream toolkit's inline pin (``repomatic==X.Y.Z``) is the exception:
    it is aligned to the newest `uses:` ref version instead of the newest
    cooldown-eligible PyPI release. The refs are its source of truth: they are
    written by `repomatic init` alone, which weighs the cooldown itself (see
    {func}`~repomatic.init_project.resolve_default_pin`) and is skipped here and
    in :func:`_resolve_action_pins`. Re-judging their outcome against PyPI would
    leave `lint-repo`'s lockstep check red for a full cooldown window after
    every refs bump.
    """
    min_age = parse_min_age(rc.config.minimum_release_age)
    today = rc.today
    plan = SyncPlan(
        operation="sync-workflow-pins", subject="Package", heading="Updated packages"
    )
    plan.reference_date = today
    file_data = _pinnable_files()

    # Highest currently-pinned version per (ecosystem, package).
    current: dict[tuple[str, str], str] = {}
    for text in file_data.values():
        for literal in find_workflow_literals(text):
            key = (literal.ecosystem, literal.package)
            if key not in current or is_newer(literal.version, current[key]):
                current[key] = literal.version

    # Newest upstream `uses:` ref version, for the lockstep alignment.
    upstream_package = UPSTREAM_PACKAGE
    lockstep_version: str | None = None
    for text in file_data.values():
        for version in find_upstream_ref_versions(text, DEFAULT_REPO):
            if lockstep_version is None or is_newer(version, lockstep_version):
                lockstep_version = version

    resolved: dict[tuple[str, str], str] = {}
    for (ecosystem, package), current_version in sorted(current.items()):
        if (
            ecosystem == "pypi"
            and package == upstream_package
            and lockstep_version is not None
        ):
            # Lockstep alignment, in either direction and regardless of the
            # cooldown. Resolved unconditionally so every literal of the
            # package realigns, even a straggler file lagging behind an
            # already-aligned one; equal pins rewrite to themselves, which
            # records no change. No held-back entry either: the pin tracks
            # the refs, not PyPI, which also means no PyPI upload date is
            # fetched: the "Released" cell marks the exemption instead.
            resolved[(ecosystem, package)] = lockstep_version
            plan.name_urls[package] = PYPI_PACKAGE_URL.format(package=package)
            docs_url = template_docs_url("sync-workflow-pins")
            marker = "⛓️ lockstep with `uses:` refs"
            plan.released_overrides[package] = (
                f"[{marker}]({docs_url})" if docs_url else marker
            )
            continue
        candidates = (
            npm_candidates(package) if ecosystem == "npm" else pypi_candidates(package)
        )
        if ecosystem == "pypi" and package == SETUP_UV_PACKAGE:
            candidates = _gate_uv_on_checksums(
                candidates, current_version, file_data, min_age, today
            )
        latest = select_latest(candidates, min_age, today)
        # Audit the pin already on disk, not just the one about to replace it.
        # A pin inside the window resolves through `uvx` in CI, which can read
        # no per-package exemption, so it fails every job that installs it.
        stuck = pin_inside_cooldown(candidates, current_version, min_age, today)
        if stuck:
            logging.warning(
                f"{package}=={current_version} is pinned inside the"
                f" {rc.config.minimum_release_age} cooldown (published"
                f" {stuck.isoformat()}). Installs resolving it from an index"
                " will fail until it ages out."
            )
        package_url = (
            NPM_PACKAGE_URL.format(package=package)
            if ecosystem == "npm"
            else PYPI_PACKAGE_URL.format(package=package)
        )
        _track_held_back(
            plan,
            rc,
            package,
            package_url,
            candidates,
            latest,
            current_version,
            min_age,
        )
        if latest is None or not is_newer(latest.version, current_version):
            continue
        resolved[(ecosystem, package)] = latest.version
        plan.dates[package] = latest.date
        plan.name_urls[package] = package_url

    # The lockstep alignment above ignores the cooldown, so the pin it writes can
    # name a release published minutes ago. Downstream workflows export a
    # blanket `UV_EXCLUDE_NEWER` that `uvx` cannot override per package from the
    # environment, so the exemption has to ride on the command line, exactly as
    # the release freeze splices it into this repository's own workflows.
    rewriter = partial(
        apply_workflow_literals,
        self_pin=(upstream_package, SELF_PIN_COOLDOWN_EXEMPTION),
    )
    _plan_file_rewrites(plan, rc, file_data, rewriter, resolved, min_age)
    if rc.release_notes:
        # Only PyPI literals resolve to a source repo, reusing sync-uv-lock's
        # path: PyPI `project_urls` to GitHub releases, with a changelog-link
        # fallback. npm literals have no discovery path, so they are left out.
        pypi_packages = {pkg for eco, pkg in resolved if eco == "pypi"}
        pypi_changes = [c for c in plan.changes if c[0] in pypi_packages]
        if pypi_changes:
            notes = fetch_release_notes(pypi_changes)
            plan.notes_section = format_release_notes(notes)
    return plan


def _apply_file_writes(plan: SyncPlan) -> None:
    """Rewrite each planned file from its current on-disk text.

    Each file is re-read and the operation's rewriter replayed on it (see
    {attr}`SyncPlan.rebase`), so a sibling operation's apply landing between
    this plan's resolve and its write survives. Plans without a rebase
    closure fall back to writing the resolve-time text verbatim.
    """
    for path, planned in plan.file_writes.items():
        if plan.rebase is None:
            path.write_text(planned, encoding="UTF-8")
            continue
        current = path.read_text(encoding="UTF-8")
        rebased, _changes = plan.rebase(current)
        if rebased != current:
            path.write_text(rebased, encoding="UTF-8")


def _dep_sources_applies() -> bool:
    """`sync-dep-sources` runs wherever a git branch is tracked as a source."""
    return bool(tracked_git_overrides(Path("pyproject.toml")))


def _uv_lock_applies() -> bool:
    """`sync-uv-lock` runs wherever a `uv.lock` is present."""
    return Path("uv.lock").is_file()


def _tool_versions_applies() -> bool:
    """`sync-tool-versions` runs only inside the repomatic source checkout.

    It rewrites repomatic's own `tool_registry.py`, so it is meaningful only when
    that file lives under the current working tree (an editable checkout), never
    from an installed wheel or a downstream repo.
    """
    source = Path(tool_registry.__file__).resolve()
    return Path.cwd().resolve() in source.parents


def _workflow_files_present() -> bool:
    """The pin updaters run wherever workflow or composite-action files exist."""
    return bool(_workflow_and_action_files())


def render_plan_markdown(plan: SyncPlan) -> str:
    """Render a plan as the markdown PR-body section every updater shares.

    Concatenates the source-swap section (when the plan carries one), the
    diff table, any release notes, the uv cooldown-bypass section, and the
    held-back section exactly as the individual `sync-*` commands do, so
    `sync-deps` and the thin commands produce identical output for the same
    plan.

    Every other section reports what the run did to the working tree, so the
    held-back one closes the body: it is the only forward-looking section,
    listing releases the run deliberately left alone. A run that only rewrites
    `exclude-newer-package` entries moves no version at all, and leading with
    the forecast would open its PR on the releases it did not adopt instead of
    the `pyproject.toml` hunk it asks to merge.
    """
    swaps = plan.uv_project.source_swaps
    swap_section = format_swap_section(
        swaps,
        name_urls=pypi_name_urls([(swap.name, "", "") for swap in swaps]),
        reference_date=plan.reference_date,
    )
    diff_table = format_diff_table(
        plan.changes,
        upload_times=plan.dates,
        cooldown_note=plan.cooldown_note,
        comparison_urls=plan.comparison_urls,
        reference_date=plan.reference_date,
        name_urls=plan.name_urls,
        heading=plan.heading,
        subject=plan.subject,
        released_overrides=plan.released_overrides,
    )
    held_back_section = format_held_back_table(
        plan.held_back,
        plan.held_back_note,
        name_urls=plan.held_back_name_urls,
        subject=plan.subject,
    )
    # Bypasses are always PyPI packages (a uv-only concept), so their link
    # targets are derived here rather than carried on the plan.
    uv = plan.uv_project
    bypass_names = [
        *(forecast.name for forecast in uv.bypass_forecasts),
        *(forecast.name for forecast in uv.pruned_bypasses),
        *uv.frozen_bypasses,
    ]
    bypass_section = format_bypass_section(
        uv.bypass_forecasts,
        pruned=uv.pruned_bypasses,
        frozen=uv.frozen_bypasses,
        name_urls=pypi_name_urls([(name, "", "") for name in bypass_names]),
    )
    # A splice-only run moves no version, so it reaches the diff table with
    # nothing to put in it. Without this section its PR would carry a workflow
    # hunk and no prose saying why one command grew a flag.
    exemption_section = ""
    if plan.self_pin_exemptions:
        files = ", ".join(f"`{name}`" for name in sorted(plan.self_pin_exemptions))
        exemption_section = (
            "## 🩹 Restored cooldown exemption\n\n"
            f"Spliced the missing `--exclude-newer-package` flag into {files}. The"
            " inline pin moves in lockstep with the `uses:` refs, so it names a"
            " release younger than the workflow-wide `UV_EXCLUDE_NEWER` window and"
            " could not resolve at all without the exemption."
        )
    return "\n\n".join(
        section
        for section in (
            swap_section,
            diff_table,
            plan.notes_section,
            bypass_section,
            exemption_section,
            held_back_section,
        )
        if section
    )


def print_sync_table(
    ctx: Context,
    changes: list[tuple[str, str, str]],
    dates: dict[str, str],
    *,
    subject: str,
    reference_date: date,
) -> None:
    """Print the shared terminal table for the dependency updaters.

    Columns are `{subject} | Old | New | Released`, the released date carrying a
    relative hint. Shared by `sync-uv-lock` and the three `sync-*` commands so
    their terminal output matches, and respects the global `--table-format`.
    Old/New stay separate columns (not the merged `Change` cell of the markdown
    PR body) so structured `--table-format json`/`csv` output stays parseable.
    """
    show_released = bool(dates)
    headers: tuple[str, ...] = (
        (subject, "Old", "New", "Released")
        if show_released
        else (subject, "Old", "New")
    )
    rows: list[tuple[str, ...]] = []
    for name, old, new in changes:
        row: tuple[str, ...] = (name, old or "(new)", new or "(removed)")
        if show_released:
            row = (*row, format_released(dates.get(name, ""), reference_date))
        rows.append(row)
    ctx.print_table(rows, headers)


def print_held_back_table(
    ctx: Context,
    held_back: list[HeldBackPackage],
    *,
    subject: str = "Package",
) -> None:
    """Print the shared held-back terminal table for the cooldown-gated updaters.

    Columns are *subject* followed by
    {data}`~repomatic.dep_report.HELD_BACK_COLUMNS`. Shared by `sync-uv-lock`
    and the three `sync-*` commands, and respects the global `--table-format`.
    """
    echo("Held back by cooldown:")
    headers = (subject, *HELD_BACK_COLUMNS)
    rows = [
        (
            pkg.name,
            pkg.locked_version,
            pkg.available_version,
            pkg.released,
            pkg.eligible,
        )
        for pkg in held_back
    ]
    ctx.print_table(rows, headers)


def print_bypass_table(ctx: Context, forecasts: list[BypassForecast]) -> None:
    """Print the active cooldown-bypass freezes with their expiry forecasts.

    Columns are {data}`~repomatic.dep_report.BYPASS_COLUMNS`, mirroring the
    markdown section from {func}`~repomatic.dep_report.format_bypass_section`,
    and respects the global `--table-format`.
    """
    echo("Cooldown bypasses:")
    headers = BYPASS_COLUMNS
    rows = [
        (forecast.name, forecast.held_version, forecast.expires)
        for forecast in forecasts
    ]
    ctx.print_table(rows, headers)


def print_plan_tables(ctx: Context, plan: SyncPlan, reference_date: date) -> None:
    """Print a resolved plan's diff, bypass and held-back tables.

    The terminal counterpart of {func}`render_plan_markdown`, deliberately
    beside it and in the same order, so a run's terminal output and its PR
    body read the same way and cannot drift apart. Every dependency updater
    goes through here: the two lockfile commands, the three version-sync
    commands, and the aggregate `sync-deps`.

    Each table respects the global `--table-format`, and an empty section
    prints nothing.
    """
    print_sync_table(
        ctx,
        plan.changes,
        plan.dates,
        subject=plan.subject,
        reference_date=reference_date,
    )
    if plan.uv_project.bypass_forecasts:
        print_bypass_table(ctx, plan.uv_project.bypass_forecasts)
    if plan.held_back:
        print_held_back_table(ctx, plan.held_back, subject=plan.subject)


def emit_lockfile_sync_report(
    ctx: Context,
    plan: SyncPlan,
    *,
    reference_date: date,
    table: bool,
    output: Path | None,
    output_format: str,
) -> None:
    """Emit the terminal tables and markdown report of a lockfile sync.

    `sync-uv-lock` and `sync-dep-sources` share this tail. They alone can suppress
    the terminal tables with `--no-table` (their CI jobs want only the markdown
    report), and they alone have an `exclude-newer` cutoff to announce, uv's
    lock-level cooldown standing in for the `minimum-release-age` window the
    version-sync trio reports.
    """
    if table:
        if plan.uv_project.exclude_newer:
            echo(
                "exclude-newer cutoff: "
                f"{format_upload_date(plan.uv_project.exclude_newer)}"
            )
        print_plan_tables(ctx, plan, reference_date)

    # Release notes echoed to the terminal (already fetched during resolve).
    if plan.notes_section:
        echo("")
        echo(plan.notes_section)

    # File output: markdown report for CI or downstream tooling.
    if output:
        emit_report(render_plan_markdown(plan), output, output_format)


def emit_version_sync_report(
    ctx: Context,
    plan: SyncPlan,
    output: Path | None,
    output_format: str,
) -> None:
    """Print a terminal report and optionally write a markdown PR-body report.

    Shared by the three `sync-*` version updaters. The terminal table and the
    markdown PR body (diff table, held-back section, release notes) route
    through the same shared renderers `sync-uv-lock` and `sync-deps` use
    ({func}`render_plan_markdown`), so every dependency updater's report
    matches.
    """
    echo(f"{len(plan.changes)} {plan.subject.lower()}(s) updated.")
    if plan.self_pin_exemptions:
        echo(
            "Backfilled the cooldown exemption on the self-pin in: "
            + ", ".join(sorted(plan.self_pin_exemptions))
        )
    if plan.cutoff is not None:
        echo(f"minimum-release-age cutoff: {plan.cutoff:%Y-%m-%d}")
    print_plan_tables(
        ctx, plan, plan.reference_date or datetime.now(timezone.utc).date()
    )
    if plan.notes_section:
        echo("")
        echo(plan.notes_section)
    if output:
        emit_report(render_plan_markdown(plan), output, output_format)


def run_version_sync(
    ctx: Context,
    op_name: str,
    output: Path | None,
    output_format: str,
    release_notes: bool,
    held_back: bool,
    up_to_date: str,
) -> None:
    """Shared body of the three version-sync commands.

    `sync-tool-versions`, `sync-action-pins`, and `sync-workflow-pins` differ
    only in their operation, feature flag, and messages: the resolve, apply,
    and report sequence is identical. The feature-flag guard stays with each
    command, which knows its own `[tool.repomatic]` key.

    :param ctx: The Click context, exited with `0` when nothing needs updating.
    :param op_name: The {data}`OPERATIONS_BY_NAME` key.
    :param output: The `--output` report path.
    :param output_format: The `--output-format` value.
    :param release_notes: Whether to fetch GitHub release notes.
    :param held_back: Whether to report cooldown-held releases.
    :param up_to_date: Message printed when nothing needs updating.
    """
    op = OPERATIONS_BY_NAME[op_name]
    rc = ResolveContext(
        config=get_tool_config(ctx),
        today=datetime.now(timezone.utc).date(),
        release_notes=release_notes,
        held_back=held_back,
    )
    plan = op.resolve(rc)

    if not plan.has_changes:
        echo(up_to_date)
        ctx.exit(0)

    op.apply(plan)

    emit_version_sync_report(ctx, plan, output, output_format)


def resolve_lockfile_plan(
    op_name: str,
    config: Config,
    *,
    lockfile: Path,
    table: bool,
    output: Path | None,
    release_notes: bool,
    held_back: bool,
) -> tuple[SyncOperation, ResolveContext, SyncPlan]:
    """Resolve one of the two lockfile-mutating operations.

    `sync-uv-lock` and `sync-dep-sources` build the same resolve context and,
    unlike the version-sync trio, gate the held-back probe on a consumer being
    present: that probe costs a second full uv resolution, so a run that prints
    no table and writes no report must not pay for it.

    The apply and the narration stay with each command, whose "what happened"
    lines differ (adopted releases for one, bypass lifecycle for the other).

    :return: `(operation, resolve_context, plan)`.
    """
    op = OPERATIONS_BY_NAME[op_name]
    rc = ResolveContext(
        config=config,
        today=datetime.now(timezone.utc).date(),
        release_notes=release_notes,
        held_back=held_back and (table or output is not None),
        lockfile=lockfile,
    )
    return op, rc, op.resolve(rc)


@dataclass(frozen=True)
class SyncOperation:
    """One cooldown-respecting dependency updater, as data.

    Naming rule 3 (`claude.md`): the CLI command, workflow job ID, PR branch,
    and PR-body template all share {attr}`name`. The CI-only metadata
    ({attr}`job_name`, {attr}`job_if`, {attr}`editable`, {attr}`needs_gh_token`,
    {attr}`ci_flags`) lets {mod}`repomatic.github.workflow_sync` emit the
    consolidated job without a hand-maintained YAML twin.
    """

    name: str
    """Command, job ID, branch, and template name (all identical)."""

    config_flag: str
    """The {class}`~repomatic.config.Config` boolean gating this operation."""

    job_name: str
    """Human-facing CI job step name, with its emoji (`⛓️ Sync uv.lock`)."""

    job_if: str
    """The workflow `if:` expression gating the CI steps (empty for none)."""

    resolve: Callable[[ResolveContext], SyncPlan]
    """Read phase: network discovery, returns a {class}`SyncPlan`."""

    apply: Callable[[SyncPlan], None]
    """Write phase: persist the plan's file writes."""

    applies_here: Callable[[], bool]
    """Whether the operation is meaningful in the current working tree."""

    write_domain: tuple[str, ...]
    """Human-readable globs the operation mutates (for conflict awareness)."""

    workflow: str = "autofix.yaml"
    """Workflow file whose job runs this operation in CI."""

    job: str = "sync-deps"
    """Job ID inside {attr}`workflow` hosting this operation's steps.

    Defaults to the consolidated `sync-deps` job, which shares one checkout
    across every bumper whose write domain exists downstream. An operation that
    writes only to this repository's own source belongs in a job of its own, in
    a workflow `repomatic init` never materializes downstream (see
    {data}`~repomatic.registry.SELF_MAINTENANCE_WORKFLOWS`).
    """

    editable: bool = False
    """CI install mode: `uv run --frozen` (rewrites source) vs `uvx --from .`."""

    needs_gh_token: bool = False
    """Whether the CI step needs `GH_TOKEN` for the GitHub releases API."""

    ci_flags: tuple[str, ...] = ()
    """Extra CLI flags the consolidated CI job passes to the command."""

    @property
    def branch(self) -> str:
        """The PR branch name (identical to {attr}`name`)."""
        return self.name

    @property
    def template(self) -> str:
        """The PR-body template name (identical to {attr}`name`)."""
        return self.name

    @property
    def consolidated(self) -> bool:
        """Whether this operation shares the multi-bumper `sync-deps` job.

        A consolidated operation must reset the working tree before it runs, so
        the previous bumper's diff never bleeds into its PR. An operation with a
        job to itself starts from a clean checkout and needs no reset.

        Compared against the {attr}`job` field default rather than against a
        repeated `"sync-deps"` literal, so renaming the shared job is a one-line
        change.
        """
        return self.job == SyncOperation.job

    def is_enabled(self, config: Config) -> bool:
        """Whether this operation is enabled in *config*."""
        return bool(getattr(config, self.config_flag))


SYNC_OPERATIONS: tuple[SyncOperation, ...] = (
    SyncOperation(
        name="sync-dep-sources",
        config_flag="dep_sources_sync",
        job_name="🔀 Sync dependency sources",
        job_if="fromJSON(needs.metadata.outputs.metadata).is_python_project",
        resolve=_resolve_dep_sources,
        apply=_apply_dep_sources,
        applies_here=_dep_sources_applies,
        write_domain=("uv.lock", "pyproject.toml"),
        ci_flags=("--no-table", "--release-notes"),
    ),
    SyncOperation(
        name="sync-uv-lock",
        config_flag="uv_lock_sync",
        job_name="⛓️ Sync uv.lock",
        job_if="fromJSON(needs.metadata.outputs.metadata).is_python_project",
        resolve=_resolve_uv_lock,
        apply=_apply_uv_lock,
        applies_here=_uv_lock_applies,
        write_domain=("uv.lock", "pyproject.toml [tool.uv]"),
        ci_flags=("--no-table", "--release-notes"),
    ),
    SyncOperation(
        name="sync-action-pins",
        config_flag="action_pins_sync",
        job_name="📌 Sync action pins",
        job_if="fromJSON(needs.metadata.outputs.metadata).workflow_files",
        resolve=_resolve_action_pins,
        apply=_apply_file_writes,
        applies_here=_workflow_files_present,
        write_domain=(".github/workflows/*.yaml", ".github/actions/**/*.yaml"),
        needs_gh_token=True,
        ci_flags=("--release-notes",),
    ),
    SyncOperation(
        name="sync-workflow-pins",
        config_flag="workflow_pins_sync",
        job_name="🔖 Sync workflow pins",
        job_if="fromJSON(needs.metadata.outputs.metadata).workflow_files",
        resolve=_resolve_workflow_pins,
        apply=_apply_file_writes,
        applies_here=_workflow_files_present,
        write_domain=(".github/workflows/*.yaml", ".github/actions/**/*.yaml"),
        needs_gh_token=True,
        ci_flags=("--release-notes",),
    ),
    SyncOperation(
        name="sync-tool-versions",
        config_flag="tool_versions_sync",
        job_name="🔼 Sync tool versions",
        # No `if:` gate: this one lives in the upstream-only self-maintenance.yaml,
        # which downstream repos never receive, so nothing has to be guarded away
        # at runtime.
        job_if="",
        resolve=_resolve_tool_versions,
        apply=_apply_tool_versions,
        applies_here=_tool_versions_applies,
        write_domain=("repomatic/tool_registry.py", ".github/workflows/lint.yaml"),
        workflow="self-maintenance.yaml",
        job="sync-tool-versions",
        editable=True,
        needs_gh_token=True,
        ci_flags=("--release-notes",),
    ),
)
"""The cooldown-respecting dependency updaters, in CI execution order.

`sync-dep-sources` first (adopting a release changes what the routine re-lock
even does), then `sync-uv-lock` (its lock churn gates other Python work), then
the two workflow-file rewriters, then the upstream-only tool bump last.

Only the first four share the `sync-deps` job in `autofix.yaml`. `sync-tool-versions`
runs from `self-maintenance.yaml` on its own daily schedule, since it rewrites this
package's source and has no downstream meaning; the order still applies to a local
`repomatic sync-deps`, which runs every enabled operation in one pass.
"""

OPERATIONS_BY_NAME: dict[str, SyncOperation] = {op.name: op for op in SYNC_OPERATIONS}
"""{data}`SYNC_OPERATIONS` keyed by {attr}`SyncOperation.name`."""


def selected_operations(
    config: Config,
    *,
    here_only: bool = True,
    names: Sequence[str] | None = None,
) -> list[SyncOperation]:
    """Return the operations to run, in {data}`SYNC_OPERATIONS` order.

    The config feature flags are always authoritative: a disabled operation is
    dropped whether or not it was named (mirrors each standalone `sync-*`
    command, which exits when its flag is off).

    :param config: The resolved configuration; disabled operations are dropped.
    :param here_only: Drop operations whose {attr}`SyncOperation.applies_here`
        is false (no `uv.lock`, no workflow files, not the repomatic checkout).
        Ignored when *names* is given: naming an operation is an explicit opt-in
        that bypasses the working-tree probe (the "scope exclusions are
        defaults, not absolutes" rule in `claude.md`).
    :param names: When given, restrict to these operation names. Unknown names
        are ignored (the CLI validates them upstream).
    """
    if names:
        chosen = [
            OPERATIONS_BY_NAME[name] for name in names if name in OPERATIONS_BY_NAME
        ]
        return operation_order(op for op in chosen if op.is_enabled(config))
    return [
        op
        for op in SYNC_OPERATIONS
        if op.is_enabled(config) and (not here_only or op.applies_here())
    ]


def run_sync_operations(
    operations: Sequence[SyncOperation],
    rc: ResolveContext,
    *,
    spinner_label: str | None = None,
) -> list[tuple[SyncOperation, SyncPlan | None]]:
    """Resolve operations concurrently, then apply them serially.

    The resolve phase fans out through {func}`click_extra.run_jobs` (the work is
    network-bound and disjoint per operation), sized by the global `--jobs`
    option and sequential when no CLI context is active (as in tests). At
    `DEBUG` verbosity the fan-out also collapses to sequential so per-operation
    log narration stays coherent, and a Ctrl+C drops queued resolves instead of
    waiting for them. When labelled, an {class}`click_extra.OperationTrail`
    reports each resolve as a `✓`/`✘` line and closes with a summary, its
    rendering tracking the resolved worker count and its elapsed times following
    `--time` (click-extra's own default). The apply phase runs in
    {data}`SYNC_OPERATIONS` order because three of the five rewrite the same
    workflow files. In `--dry-run` no apply runs. An operation whose resolve
    raises is logged and reported with a `None` plan so one failure never blocks
    the others.

    :param operations: The operations to run (already filtered by the caller).
    :param rc: Shared resolve inputs.
    :param spinner_label: Present-tense label for the resolve trail (like
        `"Resolving dependency updates"`). When set and attached to a TTY, the
        trail shows a `✓`/`✘` line per operation and a running tally; unset
        (programmatic and test calls) forces it silent, so CI and tests show
        nothing.
    :return: Each operation paired with its plan (or `None` if its resolve
        failed), in {data}`SYNC_OPERATIONS` order.
    """
    if not operations:
        return []

    # Resolve the fan-out width once, up front, so the trail's rendering mode
    # (an aggregate spinner when concurrent, echoed lines when serial) matches
    # the width run_jobs actually fans out to below.
    ctx = click.get_current_context(silent=True)
    jobs = resolve_jobs(ctx, len(operations), serial_at_debug=True)

    # A trail is opt-in: without a label (programmatic and test calls) it stays
    # forced-silent, so only the CLI's `spinner_label` lights it up.
    trail = OperationTrail(
        label=spinner_label or "",
        unit="operations",
        total=len(operations),
        jobs=jobs,
        enabled=None if spinner_label else False,
    )

    def resolve_and_mark(op: SyncOperation) -> SyncPlan | None:
        try:
            plan = op.resolve(rc)
        except Exception:
            logging.exception(f"{op.name} failed to resolve.")
            plan = None
        trail.mark(plan is not None, op.name)
        return plan

    with trail:
        plans = {
            op.name: plan
            for op, plan in zip(
                operations, run_jobs(resolve_and_mark, operations, jobs=jobs)
            )
        }
        trail.finish(
            trail.ok_count == len(operations),
            f"Resolved {trail.ok_count}/{len(operations)} operations",
        )

    if not rc.dry_run:
        for op in operations:
            plan = plans.get(op.name)
            if plan is not None and plan.has_changes:
                op.apply(plan)

    return [(op, plans.get(op.name)) for op in operations]


def operation_order(operations: Iterable[SyncOperation]) -> list[SyncOperation]:
    """Sort *operations* into {data}`SYNC_OPERATIONS` order."""
    index = {op.name: i for i, op in enumerate(SYNC_OPERATIONS)}
    return sorted(operations, key=lambda op: index[op.name])
