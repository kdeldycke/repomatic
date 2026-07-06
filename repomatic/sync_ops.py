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

The four `sync-*` dependency bumpers (`sync-uv-lock`, `sync-tool-versions`,
`sync-action-pins`, `sync-workflow-pins`) share a shape: discover the latest
eligible upstream version, gated by the `[tool.repomatic] minimum-release-age`
cooldown (or uv's `exclude-newer` for the lock), then rewrite the pin. This
module turns that shape into data: one {class}`SyncOperation` per bumper, in
{data}`SYNC_OPERATIONS`.

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
  the repository, so the four resolves are safe to run in parallel.
- {attr}`SyncOperation.apply` writes the planned contents. Three of the four
  operations rewrite `.github/workflows/*.yaml` (action pins, workflow literals,
  and the actionlint matcher URL all live there), so applies must run serially.

`sync-uv-lock` is the documented exception: its discovery *is* a mutation
(`uv lock --upgrade` rewrites `uv.lock`), so {attr}`SyncOperation.resolve`
writes during the parallel phase and {attr}`SyncOperation.apply` is a no-op.
This is safe because its write domain (`uv.lock`, `pyproject.toml`'s
`[tool.uv]`) is disjoint from every other operation's. A `--dry-run` resolve
snapshots and restores those two files so the preview leaves no trace.

The datasource adapters, version selection, and pure string rewriters live in
{mod}`repomatic.version_sync` and {mod}`repomatic.uv`; this module composes them
with the file I/O and checksum recompute. Terminal and PR-body rendering stay in
{mod}`repomatic.cli`, fed from the {class}`SyncPlan`.
"""

from __future__ import annotations

import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from click_extra import Spinner, run_jobs

from . import tool_runner
from .checksums import update_registry_checksums
from .github.releases import fetch_github_release_notes, resolve_tag_to_sha
from .init_project import init_config
from .registry import UPSTREAM_REPO_SLUGS
from .tool_runner import TOOL_REGISTRY
from .uv import (
    EXCLUDE_NEWER_HELD_BACK_NOTE,
    BypassForecast,
    HeldBackPackage,
    build_comparison_urls,
    build_held_back,
    compute_held_back_packages,
    fetch_release_notes,
    format_bypass_section,
    format_diff_table,
    format_exclude_newer_note,
    format_held_back_table,
    format_release_notes,
    pypi_name_urls,
    sync_uv_lock,
)
from .version_sync import (
    MIN_AGE_HELD_BACK_NOTE,
    apply_action_pins,
    apply_workflow_literals,
    find_action_pins,
    find_workflow_literals,
    format_cooldown_note,
    github_candidates,
    is_newer,
    npm_candidates,
    parse_min_age,
    pypi_candidates,
    select_held_back,
    select_latest,
    set_tool_version,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .config import Config

DEPENDENCY_LABEL = "🔗 dependencies"
"""GitHub label applied to every dependency-update PR.

Shared by all four bumpers so a single label filters the whole family.
"""


def workflow_and_action_files() -> list[Path]:
    """Collect workflow and composite-action YAML files under `.github/`."""
    github_dir = Path(".github")
    files: list[Path] = []
    for pattern in (
        "workflows/*.yaml",
        "workflows/*.yml",
        "actions/**/*.yaml",
        "actions/**/*.yml",
    ):
        files.extend(github_dir.glob(pattern))
    return sorted(set(files))


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
    """Noun after `### 🆙 ` in the diff table (`Updated tools`, …)."""

    changes: list[tuple[str, str, str]] = field(default_factory=list)
    """Applied `(name, old, new)` triples, in the order the report renders."""

    dates: dict[str, str] = field(default_factory=dict)
    """Name to release/upload date (`YYYY-MM-DD` or ISO 8601) for the table."""

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
    """Path to its new full text, written verbatim by {attr}`SyncOperation.apply`."""

    # `sync-tool-versions` extras: applied after the source rewrite.
    binary_overrides: dict[str, str] = field(default_factory=dict)
    """Binary tool name to new version, for the checksum recompute."""

    actionlint_version: str | None = None
    """New actionlint version, for the matcher-URL realignment."""

    checksums_path: Path | None = None
    """The `tool_runner.py` path the checksum recompute rewrites."""

    # `sync-uv-lock` extras (resolve already wrote; these only inform rendering).
    exclude_newer: str = ""
    """The `exclude-newer` cutoff from the lock, or empty."""

    reverted: bool = False
    """Whether a cosmetic-only re-lock was discarded."""

    pins_synced: bool = False
    """Whether the `[tool.uv]` policy pins were refreshed from the template."""

    pruned_bypasses: list[str] = field(default_factory=list)
    """Expired `exclude-newer-package` entries removed from `pyproject.toml`."""

    frozen_bypasses: list[str] = field(default_factory=list)
    """`exclude-newer-package` entries rewritten into freeze cutoffs."""

    bypass_forecasts: list[BypassForecast] = field(default_factory=list)
    """Active cooldown-bypass freezes with their expiry forecasts."""

    @property
    def has_changes(self) -> bool:
        """Whether the operation found anything to update.

        Cooldown-bypass edits count: a run that only prunes or freezes
        `exclude-newer-package` entries still rewrites `pyproject.toml` and
        must produce a report explaining that hunk.
        """
        return bool(self.changes or self.pruned_bypasses or self.frozen_bypasses)


def _resolve_uv_lock(rc: ResolveContext) -> SyncPlan:
    """Re-lock dependencies and roll cooldown overrides forward.

    Unlike the version-sync trio, the discovery here is the mutation:
    `uv lock --upgrade` rewrites `uv.lock` in place. The new contents are left
    on disk (the write domain is disjoint from every other operation), so
    {func}`_apply_uv_lock` is a no-op. A `--dry-run` resolve snapshots `uv.lock`
    and `pyproject.toml` up front and restores them in a `finally`.
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
    lock_before = lockfile.read_bytes() if lockfile.exists() else None
    pyproject_before = pyproject_path.read_bytes() if pyproject_path.exists() else None
    try:
        # Sync repomatic's canonical [tool.uv] policy pins from the bundled
        # template before re-locking, so a bumped exclude-newer feeds the
        # resolution and a bumped required-version rides in the same change.
        if pyproject_path.exists():
            merged = init_config("uv", pyproject_path)
            if merged is not None:
                pyproject_path.write_text(merged, encoding="UTF-8")
                plan.pins_synced = True

        result = sync_uv_lock(lockfile)
        plan.changes = result.changes
        plan.dates = result.upload_times
        plan.exclude_newer = result.exclude_newer
        plan.reverted = result.reverted
        plan.cooldown_note = format_exclude_newer_note(result.exclude_newer)
        plan.name_urls = pypi_name_urls(result.changes)
        plan.pruned_bypasses = result.pruned_bypasses
        plan.frozen_bypasses = result.frozen_bypasses
        plan.bypass_forecasts = result.bypass_forecasts

        # The probe runs a second uv resolution, so skip it with nothing to
        # report: the caller already gates `held_back` on a consumer being
        # present (terminal table or file output). Bypass edits count as
        # changes: a prune may leave newer releases cooldown-held, and this
        # is the report that explains them.
        if rc.held_back and plan.has_changes:
            plan.held_back = compute_held_back_packages(lockfile)
            plan.held_back_name_urls = pypi_name_urls([
                (pkg.name, "", "") for pkg in plan.held_back
            ])

        notes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
        if rc.release_notes and result.changes:
            notes = fetch_release_notes(result.changes)
            plan.notes_section = format_release_notes(notes)
        plan.comparison_urls = build_comparison_urls(result.changes, notes)
    finally:
        if rc.dry_run:
            if lock_before is not None:
                lockfile.write_bytes(lock_before)
            if pyproject_before is not None:
                pyproject_path.write_bytes(pyproject_before)
    return plan


def _apply_uv_lock(plan: SyncPlan) -> None:
    """No-op: {func}`_resolve_uv_lock` already wrote `uv.lock`."""


def _resolve_tool_versions(rc: ResolveContext) -> SyncPlan:
    """Bump every `repomatic run` tool to its latest eligible release."""
    min_age = parse_min_age(rc.config.minimum_release_age)
    today = rc.today
    plan = SyncPlan(
        operation="sync-tool-versions", subject="Tool", heading="Updated tools"
    )
    plan.reference_date = today
    tool_runner_path = Path(tool_runner.__file__)
    content = tool_runner_path.read_text(encoding="UTF-8")

    changes: list[tuple[str, str, str]] = []
    overrides: dict[str, str] = {}
    held_back_pkgs: list[HeldBackPackage] = []
    for name, spec in sorted(TOOL_REGISTRY.items()):
        if spec.binary is not None:
            if not spec.source_url:
                continue
            candidates = github_candidates(spec.source_url, spec.tag_pattern)
        elif spec.npm is not None:
            candidates = npm_candidates(spec.package or spec.name)
        else:
            candidates = pypi_candidates(spec.pypi_name)
        latest = select_latest(candidates, min_age, today)
        # Held-back covers every scanned tool, even ones not bumped this run, so
        # the section shows the full cooldown pipeline (parity with sync-uv-lock).
        final_version = (
            latest.version
            if latest is not None and is_newer(latest.version, spec.version)
            else spec.version
        )
        if rc.held_back:
            withheld = select_held_back(candidates, final_version, min_age, today)
            if withheld is not None:
                held_back_pkgs.append(
                    build_held_back(
                        name,
                        final_version,
                        withheld.version,
                        withheld.date,
                        min_age,
                        today,
                    )
                )
                plan.held_back_name_urls[name] = spec.datasource_url
        if latest is None or not is_newer(latest.version, spec.version):
            continue
        content = set_tool_version(content, name, latest.version)
        changes.append((name, spec.version, latest.version))
        plan.dates[name] = latest.date
        overrides[name] = latest.version

    plan.changes = changes
    plan.held_back = held_back_pkgs
    plan.cutoff = today - min_age if min_age else None
    plan.cooldown_note = (
        format_cooldown_note(rc.config.minimum_release_age, plan.cutoff)
        if plan.cutoff is not None
        else ""
    )
    if not changes:
        return plan

    plan.file_writes = {tool_runner_path: content}
    plan.checksums_path = tool_runner_path
    plan.binary_overrides = {
        name: version
        for name, version in overrides.items()
        if TOOL_REGISTRY[name].binary is not None
    }
    plan.actionlint_version = overrides.get("actionlint")
    plan.name_urls = {
        name: TOOL_REGISTRY[name].datasource_url
        for name, _old, _new in changes
        if TOOL_REGISTRY[name].source_url or TOOL_REGISTRY[name].npm is not None
    }

    if rc.release_notes:
        notes_items = [
            (name, src, old, new, TOOL_REGISTRY[name].tag_pattern)
            for name, old, new in changes
            if (src := TOOL_REGISTRY[name].source_url) and "github.com" in src
        ]
        plan.notes_section = format_release_notes(
            fetch_github_release_notes(notes_items)
        )
    return plan


def _apply_tool_versions(plan: SyncPlan) -> None:
    """Rewrite `tool_runner.py`, recompute checksums, realign the matcher URL."""
    if not plan.has_changes or plan.checksums_path is None:
        return
    path = plan.checksums_path
    path.write_text(plan.file_writes[path], encoding="UTF-8")
    # Recompute checksums and VERSIONS stamps for bumped binary tools in the same
    # pass, downloading at the new versions (the in-memory registry still holds
    # the pre-bump versions because the source was edited, not reimported).
    if plan.binary_overrides:
        update_registry_checksums(path, version_overrides=plan.binary_overrides)
    if plan.actionlint_version:
        _sync_actionlint_matcher_url(plan.actionlint_version)


def _resolve_action_pins(rc: ResolveContext) -> SyncPlan:
    """Bump SHA-pinned GitHub Actions across `.github/` to their latest release."""
    min_age = parse_min_age(rc.config.minimum_release_age)
    today = rc.today
    plan = SyncPlan(
        operation="sync-action-pins", subject="Action", heading="Updated actions"
    )
    plan.reference_date = today
    file_data = {
        path: path.read_text(encoding="UTF-8") for path in workflow_and_action_files()
    }

    # Highest currently-pinned version per slug, skipping repomatic-lineage refs
    # (those thin-caller pins are managed by `repomatic init`).
    current: dict[str, str] = {}
    for text in file_data.values():
        for pin in find_action_pins(text):
            if pin.slug in UPSTREAM_REPO_SLUGS:
                continue
            version = pin.ref.removeprefix("v")
            if pin.slug not in current or is_newer(version, current[pin.slug]):
                current[pin.slug] = version

    resolved: dict[str, tuple[str, str]] = {}
    held_back_pkgs: list[HeldBackPackage] = []
    for slug, current_version in sorted(current.items()):
        repo_url = f"https://github.com/{slug}"
        candidates = github_candidates(repo_url)
        latest = select_latest(candidates, min_age, today)
        # Held-back covers every scanned action, even ones not bumped this run.
        final_version = (
            latest.version
            if latest is not None and is_newer(latest.version, current_version)
            else current_version
        )
        if rc.held_back:
            withheld = select_held_back(candidates, final_version, min_age, today)
            if withheld is not None:
                held_back_pkgs.append(
                    build_held_back(
                        slug,
                        final_version,
                        withheld.version,
                        withheld.date,
                        min_age,
                        today,
                    )
                )
                plan.held_back_name_urls[slug] = repo_url
        if latest is None or not is_newer(latest.version, current_version):
            continue
        new_sha = resolve_tag_to_sha(repo_url, latest.ref)
        if not new_sha:
            logging.warning(f"Could not resolve {slug}@{latest.ref} to a commit SHA.")
            continue
        resolved[slug] = (new_sha, latest.ref)
        plan.dates[slug] = latest.date

    changes: list[tuple[str, str, str]] = []
    for path, text in file_data.items():
        new_content, file_changes = apply_action_pins(text, resolved)
        if file_changes:
            plan.file_writes[path] = new_content
            changes.extend(file_changes)
    plan.changes = sorted(set(changes))
    plan.held_back = held_back_pkgs
    plan.cutoff = today - min_age if min_age else None
    plan.cooldown_note = (
        format_cooldown_note(rc.config.minimum_release_age, plan.cutoff)
        if plan.cutoff is not None
        else ""
    )
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


def _resolve_workflow_pins(rc: ResolveContext) -> SyncPlan:
    """Bump npm and PyPI version literals embedded in workflow YAML."""
    min_age = parse_min_age(rc.config.minimum_release_age)
    today = rc.today
    plan = SyncPlan(
        operation="sync-workflow-pins", subject="Package", heading="Updated packages"
    )
    plan.reference_date = today
    file_data = {
        path: path.read_text(encoding="UTF-8") for path in workflow_and_action_files()
    }

    # Highest currently-pinned version per (ecosystem, package).
    current: dict[tuple[str, str], str] = {}
    for text in file_data.values():
        for literal in find_workflow_literals(text):
            key = (literal.ecosystem, literal.package)
            if key not in current or is_newer(literal.version, current[key]):
                current[key] = literal.version

    resolved: dict[tuple[str, str], str] = {}
    held_back_pkgs: list[HeldBackPackage] = []
    for (ecosystem, package), current_version in sorted(current.items()):
        candidates = (
            npm_candidates(package) if ecosystem == "npm" else pypi_candidates(package)
        )
        latest = select_latest(candidates, min_age, today)
        package_url = (
            f"https://www.npmjs.com/package/{package}"
            if ecosystem == "npm"
            else f"https://pypi.org/project/{package}/"
        )
        # Held-back covers every scanned literal, even ones not bumped this run.
        final_version = (
            latest.version
            if latest is not None and is_newer(latest.version, current_version)
            else current_version
        )
        if rc.held_back:
            withheld = select_held_back(candidates, final_version, min_age, today)
            if withheld is not None:
                held_back_pkgs.append(
                    build_held_back(
                        package,
                        final_version,
                        withheld.version,
                        withheld.date,
                        min_age,
                        today,
                    )
                )
                plan.held_back_name_urls[package] = package_url
        if latest is None or not is_newer(latest.version, current_version):
            continue
        resolved[(ecosystem, package)] = latest.version
        plan.dates[package] = latest.date
        plan.name_urls[package] = package_url

    changes: list[tuple[str, str, str]] = []
    for path, text in file_data.items():
        new_content, file_changes = apply_workflow_literals(text, resolved)
        if file_changes:
            plan.file_writes[path] = new_content
            changes.extend(file_changes)
    plan.changes = sorted(set(changes))
    plan.held_back = held_back_pkgs
    plan.cutoff = today - min_age if min_age else None
    plan.cooldown_note = (
        format_cooldown_note(rc.config.minimum_release_age, plan.cutoff)
        if plan.cutoff is not None
        else ""
    )
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
    """Write each planned file verbatim (the version-sync trio's write phase)."""
    for path, content in plan.file_writes.items():
        path.write_text(content, encoding="UTF-8")


def _uv_lock_applies() -> bool:
    """`sync-uv-lock` runs wherever a `uv.lock` is present."""
    return Path("uv.lock").is_file()


def _tool_versions_applies() -> bool:
    """`sync-tool-versions` runs only inside the repomatic source checkout.

    It rewrites repomatic's own `tool_runner.py`, so it is meaningful only when
    that file lives under the current working tree (an editable checkout), never
    from an installed wheel or a downstream repo.
    """
    source = Path(tool_runner.__file__).resolve()
    return Path.cwd().resolve() in source.parents


def _workflow_files_present() -> bool:
    """The pin updaters run wherever workflow or composite-action files exist."""
    return bool(workflow_and_action_files())


def render_plan_markdown(plan: SyncPlan) -> str:
    """Render a plan as the markdown PR-body section every updater shares.

    Concatenates the diff table, any release notes, the held-back section,
    and the uv cooldown-bypass section exactly as the individual `sync-*`
    commands do, so `sync-deps` and the thin commands produce identical
    output for the same plan.
    """
    diff_table = format_diff_table(
        plan.changes,
        upload_times=plan.dates,
        cooldown_note=plan.cooldown_note,
        comparison_urls=plan.comparison_urls,
        reference_date=plan.reference_date,
        name_urls=plan.name_urls,
        heading=plan.heading,
        subject=plan.subject,
    )
    held_back_section = format_held_back_table(
        plan.held_back,
        plan.held_back_note,
        name_urls=plan.held_back_name_urls,
        subject=plan.subject,
    )
    # Bypasses are always PyPI packages (a uv-only concept), so their link
    # targets are derived here rather than carried on the plan.
    bypass_names = [
        *(forecast.name for forecast in plan.bypass_forecasts),
        *plan.pruned_bypasses,
        *plan.frozen_bypasses,
    ]
    bypass_section = format_bypass_section(
        plan.bypass_forecasts,
        pruned=plan.pruned_bypasses,
        frozen=plan.frozen_bypasses,
        name_urls=pypi_name_urls([(name, "", "") for name in bypass_names]),
    )
    return "\n\n".join(
        section
        for section in (
            diff_table,
            plan.notes_section,
            held_back_section,
            bypass_section,
        )
        if section
    )


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

    def is_enabled(self, config: Config) -> bool:
        """Whether this operation is enabled in *config*."""
        return bool(getattr(config, self.config_flag))


SYNC_OPERATIONS: tuple[SyncOperation, ...] = (
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
        job_if=(
            "github.repository == 'kdeldycke/repomatic'"
            " && (github.event_name == 'schedule'"
            " || github.event_name == 'workflow_dispatch')"
        ),
        resolve=_resolve_tool_versions,
        apply=_apply_tool_versions,
        applies_here=_tool_versions_applies,
        write_domain=("repomatic/tool_runner.py", ".github/workflows/lint.yaml"),
        editable=True,
        needs_gh_token=True,
        ci_flags=("--release-notes",),
    ),
)
"""The cooldown-respecting dependency updaters, in CI execution order.

`sync-uv-lock` first (its lock churn gates other Python work), then the two
workflow-file rewriters, then the upstream-only tool bump last.
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
    waiting for them. The apply phase runs in {data}`SYNC_OPERATIONS` order
    because three of the four rewrite the same workflow files. In `--dry-run` no
    apply runs. An operation whose resolve raises is logged and reported with a
    `None` plan so one failure never blocks the others.

    :param operations: The operations to run (already filtered by the caller).
    :param rc: Shared resolve inputs.
    :param spinner_label: When set and attached to a TTY, animate a spinner for
        the resolve phase (silent off a TTY, so CI and tests show nothing).
    :return: Each operation paired with its plan (or `None` if its resolve
        failed), in {data}`SYNC_OPERATIONS` order.
    """
    if not operations:
        return []

    def resolve_safely(op: SyncOperation) -> SyncPlan | None:
        try:
            return op.resolve(rc)
        except Exception:
            logging.exception(f"{op.name} failed to resolve.")
            return None

    progress = Spinner(spinner_label) if spinner_label else nullcontext()
    with progress:
        plans = {
            op.name: plan
            for op, plan in zip(
                operations,
                run_jobs(resolve_safely, operations, serial_at_debug=True),
            )
        }

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
