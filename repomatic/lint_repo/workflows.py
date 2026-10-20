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

"""Checks of the repository's workflow files.

The permissions, the runner images, the Python test matrix, the release-only
gates, the metadata keys a workflow asks for, and the repository-local PR
templates. The thin callers of the upstream reusable workflows get four checks
of their own: the pinned version, the triggers and the secrets they pass, and
the manual trigger every other workflow carries.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from ..checks import CheckResult
from ..components import DEFAULT_REPO
from ..frontmatter import split_frontmatter
from ..github.pr_body import footer_opted_out
from ..github.urls import repo_name
from ..github.workflow_files import (
    iter_jobs,
    jobs_of,
    literal_runners,
    steps_of,
    workflow_triggers,
)
from ..github.workflow_sync import canonical_workflow, extract_trigger_info
from ..matrix.axes import KNOWN_RUNNERS, UNSTABLE_PYTHON_VERSIONS
from ..matrix.model import PYTHON_VERSION_AXIS
from ..metadata.core import METADATA_SUBCOMMAND, Dialect, unknown_metadata_keys

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final

    from ..metadata.core import Metadata
    from .context import LintContext


PR_TEMPLATE_DIR = Path(".github/pr-templates")
"""Canonical home for a repository's own `pr-body --template-file` templates.

`.github/` already namespaces by subdirectory (`ISSUE_TEMPLATE/`, `workflows/`,
`actions/`), and a dedicated one leaves each template's basename free to carry
the operation name, so it can match its job ID and PR branch. Templates sitting
flat in `.github/` need a `pr-` prefix purely to disambiguate, which breaks that
identity and puts them next to GitHub's own `pull_request_template.md`, an
unrelated human-facing file.
"""

PYTHON_CLASSIFIER_PREFIX = "Programming Language :: Python :: "
"""Prefix of the classifiers naming a supported interpreter version.

Only the dotted ones carry a version: the bare `3` and `3 :: Only` state the
major series, and `Implementation :: CPython` the interpreter.
"""

TEMPLATE_FILE_ARG_RE = re.compile(r"--template-file[=\s]+(?P<path>\S+)")
"""A `repomatic pr-body --template-file` argument inside a workflow `run:` block.

Matched against the raw YAML text rather than the parsed document: the argument
sits inside a folded scalar, so the surrounding `run:` value is one opaque
string whichever way the file is parsed.
"""

UPSTREAM_WORKFLOW_RE: Final = re.compile(
    rf"^{re.escape(DEFAULT_REPO)}/\.github/workflows/(?P<workflow>[^@]+)@(?P<ref>.+)$"
)
"""A job-level `uses:` calling an upstream reusable workflow, with its ref.

The same references {func}`~repomatic.github.workflow_sync.canonical_workflow`
reads to tell a thin caller apart.
"""

FLOATING_UPSTREAM_REF = "main"
"""The upstream branch a thin caller must not track instead of a release."""


def check_workflow_permissions(ctx: LintContext) -> list[CheckResult]:
    """Check workflow `permissions` declarations for least privilege.

    Two failure modes are flagged:

    1. A workflow that defines its own `steps:` should carry a top-level
       `permissions` key (`permissions: {}` for least privilege) so its
       jobs default to no scopes rather than the repository default.
    2. A job that calls a reusable workflow (a job-level `uses:`) hands its
       own permissions *down*, and the reusable workflow's jobs are capped by
       them: they cannot escalate beyond what the caller grants. So under a
       top-level `permissions: {}`, a reusable-call job with no
       `permissions:` block of its own passes `{}` to the called workflow,
       and GitHub aborts the run at startup the moment a nested job requests a
       scope the caller never granted. Such a job must name the union of the
       scopes its reusable workflow needs (mirror the reusable workflow's own
       top-level `{}` plus per-job grants).

    A thin caller with *no* top-level `permissions` key is fine: its jobs
    inherit the repository default, which the reusable workflow's own
    `permissions:` blocks then cap. The failure is specifically an empty
    top-level `permissions: {}` starving an unqualified reusable call.
    """
    if not ctx.workflows:
        return [
            CheckResult(None, "Workflow permissions check: skipped (no workflows).")
        ]

    results: list[CheckResult] = []
    for wf_path, data in ctx.workflows.items():
        has_custom_steps = any("steps" in job for _name, job in jobs_of(data))
        # Reusable-workflow-calling jobs (a job-level `uses:`) that would
        # inherit an empty top-level `permissions: {}` because they declare no
        # `permissions:` of their own. `data.get("permissions") == {}` matches
        # only the empty mapping, not an absent key (which yields the repo
        # default) nor a populated one.
        starved_calls = (
            sorted(
                name
                for name, job in jobs_of(data)
                if job.get("uses") and "permissions" not in job
            )
            if data.get("permissions") == {}
            else []
        )

        # Only workflows subject to one of the two checks are reported on.
        if not has_custom_steps and not starved_calls:
            continue

        failures: list[str] = []
        if has_custom_steps and "permissions" not in data:
            failures.append(
                f"Workflow {wf_path.name} defines custom job steps but has no"
                f" top-level `permissions` key. Add `permissions: {{}}` for"
                f" least-privilege security."
            )
        if starved_calls:
            joined = ", ".join(f"`{name}`" for name in starved_calls)
            failures.append(
                f"Workflow {wf_path.name}: {joined} call a reusable workflow"
                f" under a top-level `permissions: {{}}` without their own"
                f" `permissions:` block, so GitHub rejects the run at startup"
                f" once a nested job requests a scope the caller never granted."
                f" Grant each job the union of the scopes its reusable workflow"
                f" needs."
            )

        if failures:
            results.extend(CheckResult(False, msg) for msg in failures)
        else:
            results.append(
                CheckResult(True, f"Workflow {wf_path.name}: permissions declared.")
            )

    if not results:
        results.append(
            CheckResult(
                None,
                "Workflow permissions check: no custom-step workflows found.",
            )
        )
    return results


class UpstreamLane(NamedTuple):
    """One job of a thin caller, calling an upstream reusable workflow."""

    path: Path
    """The caller's workflow file."""

    job_id: str
    """The calling job."""

    job: dict[str, Any]
    """The calling job's mapping."""

    workflow: str
    """The upstream workflow the job calls, like `lint.yaml`."""

    ref: str
    """The upstream ref the job pins, after the `@`."""

    @property
    def where(self) -> str:
        """The `file:job` location the messages name."""
        return f"{self.path.name}:{self.job_id}"


def _upstream_lanes(workflows: Mapping[Path, Mapping[str, Any]]) -> list[UpstreamLane]:
    """Every job across *workflows* that calls an upstream reusable workflow."""
    lanes = []
    for path, job_id, job in iter_jobs(workflows):
        match = UPSTREAM_WORKFLOW_RE.match(str(job.get("uses", "")))
        if match:
            lanes.append(
                UpstreamLane(path, job_id, job, match["workflow"], match["ref"])
            )
    return lanes


def check_workflow_dispatch(ctx: LintContext) -> list[CheckResult]:
    """Check that each standalone workflow can be started by hand.

    A `workflow_dispatch` trigger lets a maintainer rerun a workflow without a
    push. Two kinds of workflow are left out. A thin caller follows the
    triggers of the workflow it calls, which {func}`check_thin_caller_triggers`
    checks instead: some upstream workflows, like `cancel-runs.yaml`, have no
    manual trigger on purpose. A reusable workflow, which declares
    `workflow_call`, starts the way its callers decide.
    """
    results: list[CheckResult] = []
    for path, data in ctx.workflows.items():
        triggers = workflow_triggers(data)
        if "workflow_call" in triggers or canonical_workflow(data) is not None:
            continue
        if "workflow_dispatch" in triggers:
            results.append(
                CheckResult(True, f"{path.name}: has workflow_dispatch trigger.")
            )
        else:
            results.append(
                CheckResult(False, f"{path.name}: missing workflow_dispatch trigger.")
            )
    if not results:
        results.append(
            CheckResult(
                None, "Workflow dispatch check: skipped (no standalone workflow)."
            )
        )
    return results


def check_thin_caller_pins(ctx: LintContext) -> list[CheckResult]:
    """Check that each thin caller pins the upstream workflows to a release.

    A `@main` reference runs whatever the upstream default branch holds at the
    moment the job starts, so a change no release has tested reaches the
    repository unannounced. A tag, or a commit SHA carrying its tag as a
    comment, moves only through a reviewed pull request.
    """
    lanes = _upstream_lanes(ctx.workflows)
    if not lanes:
        return [CheckResult(None, "Thin caller pins: skipped (no thin caller).")]

    floating: dict[Path, list[str]] = {}
    for lane in lanes:
        refs = floating.setdefault(lane.path, [])
        if lane.ref == FLOATING_UPSTREAM_REF:
            refs.append(lane.job_id)

    results: list[CheckResult] = []
    for path, job_ids in floating.items():
        if job_ids:
            jobs = ", ".join(f"`{job_id}`" for job_id in job_ids)
            results.append(
                CheckResult(
                    False,
                    f"{path.name}: {jobs} use @{FLOATING_UPSTREAM_REF} instead of"
                    " a version tag.",
                )
            )
        else:
            results.append(CheckResult(True, f"{path.name}: version is pinned."))
    return results


def check_thin_caller_triggers(ctx: LintContext) -> list[CheckResult]:
    """Check that each thin caller carries the triggers of the workflow it calls.

    The caller must hold every trigger the upstream workflow declares besides
    `workflow_call`, and no other. An upstream workflow that declares none, like
    the call-only `_release-engine.yaml`, leaves the caller free to pick its
    own: the downstream `release.yaml` synthesizes `push` and
    `workflow_dispatch` for that reason.
    """
    results: list[CheckResult] = []
    for path, data in ctx.workflows.items():
        canonical = canonical_workflow(data)
        if canonical is None:
            continue
        try:
            expected = set(extract_trigger_info(canonical).non_call_triggers)
        except (FileNotFoundError, ValueError):
            results.append(
                CheckResult(
                    None,
                    f"{path.name}: calls {canonical}, which this repomatic"
                    " release does not bundle.",
                )
            )
            continue
        caller_triggers = set(workflow_triggers(data))
        missing = expected - caller_triggers
        extra = (caller_triggers - expected - {"workflow_call"}) if expected else set()
        problems: list[str] = []
        if missing:
            problems.append(f"missing: {', '.join(sorted(missing))}")
        if extra:
            problems.append(f"extra: {', '.join(sorted(extra))}")
        if problems:
            results.append(
                CheckResult(
                    False,
                    f"{path.name}: triggers diverge from canonical"
                    f" {canonical} ({'; '.join(problems)}).",
                )
            )
        else:
            results.append(CheckResult(True, f"{path.name}: triggers match canonical."))
    if not results:
        results.append(
            CheckResult(None, "Thin caller triggers: skipped (no thin caller).")
        )
    return results


def _canonical_lane(path: Path, data: Mapping[str, Any]) -> UpstreamLane | None:
    """The job of a thin caller that calls its canonical workflow.

    The canonical workflow is the one
    {func}`~repomatic.github.workflow_sync.canonical_workflow` names: the last
    upstream workflow the file calls. For the downstream `release.yaml`, that is
    the engine lane, the only one of its two lanes that declares secrets.

    :return: The calling job, or `None` when the file is no thin caller.
    """
    canonical = canonical_workflow(data)
    lanes = [
        lane for lane in _upstream_lanes({path: data}) if lane.workflow == canonical
    ]
    return lanes[-1] if lanes else None


def check_thin_caller_secrets(ctx: LintContext) -> list[CheckResult]:
    """Check that each thin caller passes every secret its canonical workflow declares.

    The secrets are read off the job that calls the canonical workflow, and
    that job alone: another job of the same file, like the build lane of the
    downstream `release.yaml`, cannot vouch for it. The job passes them as an
    explicit `secrets:` mapping, or forwards them all with `secrets: inherit`.
    """
    results: list[CheckResult] = []
    for path, data in ctx.workflows.items():
        lane = _canonical_lane(path, data)
        if lane is None:
            continue
        try:
            declared = set(extract_trigger_info(lane.workflow).call_secrets)
        except (FileNotFoundError, ValueError):
            results.append(
                CheckResult(
                    None,
                    f"{lane.where}: calls {lane.workflow}, which this repomatic"
                    " release does not bundle.",
                )
            )
            continue
        passed = lane.job.get("secrets")
        if not declared:
            results.append(
                CheckResult(
                    True, f"{lane.where}: no secrets required by {lane.workflow}."
                )
            )
        elif passed == "inherit":
            results.append(CheckResult(True, f"{lane.where}: secrets: inherit is set."))
        elif not isinstance(passed, dict):
            results.append(
                CheckResult(
                    False,
                    f"{lane.where}: no secrets passed but {lane.workflow} defines"
                    " secrets.",
                )
            )
        elif missing := declared - set(passed):
            results.append(
                CheckResult(
                    False,
                    f"{lane.where}: missing secrets for {lane.workflow}:"
                    f" {', '.join(sorted(missing))}.",
                )
            )
        else:
            results.append(
                CheckResult(
                    True, f"{lane.where}: all secrets passed to {lane.workflow}."
                )
            )
    if not results:
        results.append(
            CheckResult(None, "Thin caller secrets: skipped (no thin caller).")
        )
    return results


def check_test_matrix_excludes(ctx: LintContext) -> list[CheckResult]:
    """Flag `[tool.repomatic.test-matrix] exclude` entries that match no axis.

    An exclude naming a value absent from every matrix axis (like a renamed
    runner) can never match a combination, so `Matrix.prune()` drops it
    silently and its exclusion intent is lost. Reporting it as a warning makes
    the drift visible in CI instead of silently weakening the matrix.
    """
    metadata = ctx.md
    if not metadata.config.test_matrix.exclude:
        return [
            CheckResult(None, "Test matrix excludes check: skipped (none configured).")
        ]

    stale = metadata.stale_test_matrix_excludes
    if not stale:
        return [
            CheckResult(
                True, "Test matrix excludes check: all entries match a live axis."
            )
        ]

    results: list[CheckResult] = []
    for entry, bad in stale:
        msg = (
            f"Test matrix exclude {entry} references values absent from the "
            f"matrix axes ({bad}); it is silently dropped and never takes "
            "effect. Update it (for instance after an upstream runner rename) "
            "or remove it."
        )
        results.append(CheckResult(False, msg))
    return results


def _advertised_python_versions(metadata: Metadata) -> list[tuple[int, int]]:
    """`(major, minor)` pairs from the project's Python version classifiers.

    `3` and `3 :: Only` carry no minor version, and `Implementation :: CPython`
    is not a version at all, so only dotted numeric suffixes are kept.

    :param metadata: The repository metadata to read the classifiers from.
    :return: A sorted list of `(major, minor)` pairs, empty when none are set.
    """
    versions: set[tuple[int, int]] = set()
    for classifier in metadata.pyproject.classifiers if metadata.pyproject else []:
        suffix = classifier.removeprefix(PYTHON_CLASSIFIER_PREFIX)
        if suffix == classifier:
            continue
        major, _, minor = suffix.partition(".")
        if major.isdigit() and minor.isdigit():
            versions.add((int(major), int(minor)))
    return sorted(versions)


def _literal_python_axes(workflows: Mapping[Path, dict]) -> dict[str, list[str]]:
    """Test matrix `python-version` axes spelled out as a literal list.

    A matrix assembled at runtime, as repomatic's own
    `${{ fromJSON(...).test_matrix }}` is, reads back as an opaque string. Those
    are skipped: their axes come from {mod}`repomatic.matrix.axes` and are
    canonical by construction, so there is nothing left to reconcile.

    :param workflows: Parsed workflows, keyed by path.
    :return: A mapping of `<file>:<job>` to the versions that job names.
    """
    axes: dict[str, list[str]] = {}
    for path, job_id, job in iter_jobs(workflows):
        matrix = job.get("strategy", {}).get("matrix")
        if not isinstance(matrix, dict):
            continue
        versions = matrix.get(PYTHON_VERSION_AXIS)
        if isinstance(versions, list) and versions:
            axes[f"{path.name}:{job_id}"] = [str(v) for v in versions]
    return axes


def check_python_version_consistency(ctx: LintContext) -> list[CheckResult]:
    """Reconcile the Python versions a project requires, advertises and tests.

    The same fact is stated in up to three places, and nothing else holds them
    together: the `requires-python` lower bound, the
    `Programming Language :: Python :: X.Y` classifiers PyPI renders, and any
    test matrix naming its versions literally.

    Two failure modes are flagged:

    1. The lowest classifier disagrees with the `requires-python` floor. One of
       the two is then lying to resolvers about what installs.
    2. A literal test matrix does not reach both ends of the advertised range,
       or names a released version the classifiers never claim. Coverage of the
       *ends* is the invariant rather than of every version in between, so that
       a matrix testing the floor, the latest release and the development
       version stays conformant: skipping intermediate releases is a deliberate
       way to cut CI load, advertising an untested boundary is not.

    Versions in {data}`~repomatic.matrix.axes.UNSTABLE_PYTHON_VERSIONS` are
    exempt from the second rule, being tested precisely because they are not
    released yet and so cannot be advertised. Build flavors carrying a suffix
    (the free-threaded `3.14t`) count as their base version.
    """
    metadata = ctx.md
    advertised = _advertised_python_versions(metadata)
    if not advertised:
        return [
            CheckResult(
                None, "Python version consistency: skipped (no version classifiers)."
            )
        ]

    results: list[CheckResult] = []

    floor = min(advertised)
    declared = metadata.requires_python_floor
    if declared is None:
        results.append(
            CheckResult(
                None, "Python floor check: skipped (no requires-python lower bound)."
            )
        )
    else:
        floor_str = ".".join(map(str, floor))
        if declared != floor:
            results.append(
                CheckResult(
                    False,
                    f"requires-python floor is"
                    f" {'.'.join(map(str, declared))} but the lowest"
                    f" `Programming Language :: Python` classifier is"
                    f" {floor_str}. Resolvers read the first and humans read"
                    f" the second, so they have to agree.",
                )
            )
        else:
            results.append(
                CheckResult(
                    True,
                    f"Python floor check: requires-python and classifiers"
                    f" agree on {floor_str}.",
                )
            )

    axes = _literal_python_axes(ctx.workflows)
    if not axes:
        results.append(
            CheckResult(None, "Python test matrix: skipped (no literal axis found).")
        )
        return results

    advertised_labels = {".".join(map(str, v)) for v in advertised}
    boundaries = {
        ".".join(map(str, min(advertised))),
        ".".join(map(str, max(advertised))),
    }
    for location, versions in sorted(axes.items()):
        # A build flavor ("3.14t") exercises its base version's interpreter.
        tested = {v.rstrip("t") for v in versions}
        unadvertised = sorted(
            (tested - advertised_labels) - set(UNSTABLE_PYTHON_VERSIONS)
        )
        missing = sorted(boundaries - tested)
        if unadvertised:
            results.append(
                CheckResult(
                    False,
                    f"{location} tests Python {', '.join(unadvertised)}, which"
                    f" the classifiers do not advertise. Add the classifier, or"
                    f" drop the version from the matrix.",
                )
            )
        if missing:
            results.append(
                CheckResult(
                    False,
                    f"{location} never tests Python {', '.join(missing)}, at the"
                    f" edge of the advertised range. Testing between the ends is"
                    f" optional, reaching them is not.",
                )
            )
        if not unadvertised and not missing:
            results.append(
                CheckResult(True, f"{location}: matrix spans the advertised range.")
            )
    return results


def check_runner_images(ctx: LintContext) -> list[CheckResult]:
    """Flag runner images that move on their own, or that no axis knows about.

    Neither Dependabot nor `sync-workflow-pins` touches a `runs-on:` value:
    the first only rewrites `uses:` references, the second only the
    `uvx '<pkg>==X.Y.Z'` and `npm install pkg@X.Y.Z` literals. So only
    `sync-runner-images` moves a runner. It rewrites a literal `runs-on:` when
    GitHub retires the image or lists a newer version of it.

    Two failure modes are flagged:

    1. A `-latest` alias. GitHub repoints those to a new image on its own
       schedule, so the build changes underneath the repository with no commit
       to review, and a breakage arrives unattached to any change.
    2. An image outside the curated axes in {mod}`repomatic.matrix.axes`. Those
       carry measured guidance on speed and cost; an image picked outside them
       is one nobody has weighed, and is usually a leftover.

    Values built from an expression (`${{ matrix.os }}`) name no image here and
    are left alone: the axis they draw from is checked at its definition.
    """
    seen = literal_runners(workflows=ctx.workflows)
    if not seen:
        # The two skips are worth telling apart: no workflows at all is a
        # different repository from one whose jobs all delegate.
        if not ctx.workflows:
            return [CheckResult(None, "Runner images check: skipped (no workflows).")]
        return [
            CheckResult(None, "Runner images check: skipped (none named literally).")
        ]

    results: list[CheckResult] = []
    for runner, locations in sorted(seen.items()):
        where = ", ".join(sorted(locations))
        if runner.endswith("-latest"):
            results.append(
                CheckResult(
                    False,
                    f"{where} run on `{runner}`, which GitHub repoints without"
                    f" a commit here. Pin the image instead.",
                )
            )
        elif runner not in KNOWN_RUNNERS:
            known = ", ".join(f"`{r}`" for r in sorted(KNOWN_RUNNERS))
            results.append(
                CheckResult(
                    False,
                    f"{where} run on `{runner}`, which is not one of the images"
                    f" the test matrix axes are drawn from ({known}). It is one"
                    f" nobody has weighed for speed or cost.",
                )
            )
        else:
            results.append(CheckResult(True, f"{where}: runner `{runner}` is known."))
    return results


class ReleaseGate(NamedTuple):
    """A release-only step or job, and the project capability it needs.

    `metadata_key` names the {class}`~repomatic.metadata.core.Metadata` field that
    both decides whether the step runs and supplies what it consumes. `None`
    marks a step that needs nothing beyond being on a release commit.
    """

    name: str
    workflow: str
    metadata_key: str | None
    needs: str


RELEASE_ONLY_GATES: tuple[ReleaseGate, ...] = (
    ReleaseGate(
        "Pre-bake tag SHA",
        "_release-build.yaml",
        "cli_scripts",
        "a `[project.scripts]` entry, since click-extra's prebake finds the"
        " module to stamp through it",
    ),
    ReleaseGate(
        "📌 Tag release",
        "_release-engine.yaml",
        None,
        "nothing beyond the release commit",
    ),
    ReleaseGate(
        # Lives in the caller, not the engine, and reaches its capability gate
        # transitively: the build lane only sets `package_built` when
        # `build-package` ran, and that job is the one gated on
        # `is_python_project`. So there is no key of its own to check.
        "🐍 Publish to PyPI",
        "release.yaml",
        None,
        "a wheel from the build lane",
    ),
    ReleaseGate(
        "🐙 Create GitHub release draft",
        "_release-engine.yaml",
        None,
        "nothing beyond the release commit",
    ),
    ReleaseGate(
        "📖 Man pages",
        "_release-engine.yaml",
        "manpages_script",
        "a configured man-page script",
    ),
    ReleaseGate(
        "📎 Extra release assets",
        "_release-engine.yaml",
        "release_assets",
        "at least one configured extra asset",
    ),
    ReleaseGate(
        "🎉 Publish GitHub release",
        "_release-engine.yaml",
        None,
        "nothing beyond the release commit",
    ),
)
"""Every step and job that runs only on a release commit.

These are invisible on an ordinary push: each is gated behind a condition that
holds open only for the one commit that tags, publishes and releases. A project
can therefore build green for its entire life and meet them for the first time
on release day, where a failure costs a reverted release rather than a red push.

`VirusTotal scan` is deliberately absent: it gates on a repository secret rather
than a project capability, so there is nothing in the tree to check it against.
"""


def check_release_path(ctx: LintContext) -> list[CheckResult]:
    """Resolve the release path against this project, on an ordinary push.

    Two arms, because the two failure modes live in different repositories.

    The first runs everywhere, including downstream. It resolves each entry of
    {data}`RELEASE_ONLY_GATES` against the local project and reports which
    release-only steps a release commit would actually run. That turns a surface
    nothing exercises until release day into a line of output on every push, so
    the answer is known long before it is expensive.

    The second runs only where the reusable workflows live, since a downstream
    repository holds a thin caller and not the steps themselves. It asserts each
    gate's `if:` really does test the metadata key its step depends on. That
    invariant is what a release-only step gets wrong: the condition looks
    complete because it correctly waits for a release, while saying nothing
    about the capability the step consumes. `Pre-bake tag SHA` shipped that way,
    gated on the version alone, and every project with no `[project.scripts]`
    built green until the release commit ran prebake against a module that was
    not there.
    """
    metadata = ctx.md
    if not metadata.is_python_project:
        return [
            CheckResult(None, "Release path check: skipped (not a Python project).")
        ]

    # Resolved through `dump`, not `getattr`: several of these keys are assembled
    # by the dump factories or come from `Config`, so they are not attributes at
    # all and `getattr` would quietly return None for every one of them. Going
    # through `dump` also reads them exactly as the workflow's `fromJSON` does.
    wanted = tuple({g.metadata_key for g in RELEASE_ONLY_GATES if g.metadata_key})
    resolved = json.loads(metadata.dump(Dialect.json, keys=wanted))

    results: list[CheckResult] = []

    for gate in RELEASE_ONLY_GATES:
        if gate.metadata_key is None:
            results.append(CheckResult(True, f"Release path: `{gate.name}` will run."))
            continue
        if gate.metadata_key not in resolved:
            results.append(
                CheckResult(
                    False,
                    f"`{gate.name}` is registered against metadata key"
                    f" `{gate.metadata_key}`, which no longer exists. The"
                    f" workflow gate reading it resolves to empty, so the step"
                    f" silently never runs.",
                )
            )
            continue
        value = resolved[gate.metadata_key]
        verb = "will run" if value else "will be skipped"
        results.append(
            CheckResult(
                True,
                f"Release path: `{gate.name}` {verb} (`{gate.metadata_key}`"
                f" is {'set' if value else 'empty'}; it needs {gate.needs}).",
            )
        )

    # Second arm. Absent the reusable workflows there is nothing to read, which is
    # the normal downstream case rather than a failure.
    engine = {
        path.name: data
        for path, data in ctx.workflows.items()
        if path.name in {gate.workflow for gate in RELEASE_ONLY_GATES}
    }
    if not engine:
        return results

    # One condition walk per workflow, however many gates read it.
    indexes: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {}
    for gate in RELEASE_ONLY_GATES:
        if gate.metadata_key is None:
            continue
        data = engine.get(gate.workflow)
        if data is None:
            continue
        index = indexes.get(gate.workflow)
        if index is None:
            index = indexes[gate.workflow] = _collect_conditions(data)
        conditions = _conditions_for(index, gate.name)
        if not conditions:
            results.append(
                CheckResult(
                    False,
                    f"`{gate.name}` is registered as release-only but no step or"
                    f" job of that name carries an `if:` in {gate.workflow}."
                    f" Either the name drifted or the gate was dropped.",
                )
            )
        elif not any(gate.metadata_key in cond for cond in conditions):
            results.append(
                CheckResult(
                    False,
                    f"`{gate.name}` in {gate.workflow} needs {gate.needs}, but its"
                    f" `if:` never tests `{gate.metadata_key}`. It will fire on the"
                    f" release commit of a project that has none, which is the one"
                    f" run no ordinary push rehearses.",
                )
            )
        else:
            results.append(
                CheckResult(
                    True,
                    f"`{gate.name}` gates on `{gate.metadata_key}`.",
                )
            )

    return results


def _collect_conditions(
    workflow: dict,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Walk a workflow once, collecting every job's and step's `(name, if)`.

    The gate roster asks {func}`_conditions_for` about several names in the
    same document, so one walk feeds every question.

    :param workflow: Parsed workflow document.
    :return: `(job_pairs, step_pairs)`, each a `(name, condition)` list with
        the empty-condition entries kept so the name filter decides.
    """
    job_pairs: list[tuple[str, str]] = []
    step_pairs: list[tuple[str, str]] = []
    for _job_id, job in jobs_of(workflow):
        job_name = job.get("name", "")
        if isinstance(job_name, str):
            job_pairs.append((job_name, str(job.get("if", ""))))
        for step in steps_of(job):
            step_name = step.get("name")
            if isinstance(step_name, str):
                step_pairs.append((step_name, str(step.get("if", ""))))
    return job_pairs, step_pairs


def _conditions_for(
    index: tuple[list[tuple[str, str]], list[tuple[str, str]]],
    name: str,
) -> list[str]:
    """Collect the `if:` expressions attached to a named job or step.

    A job is matched on its `name:`, a step on its `name:` too, so the registry
    can address both by the label that shows up in the Actions UI. A job's own
    condition is returned alongside its steps', since either can be where the
    gate lives.

    :param index: A workflow's `(job_pairs, step_pairs)`, from
        {func}`_collect_conditions`.
    :param name: Job or step name; a job matches on its rendered prefix so a
        name carrying a `${{ }}` suffix still matches, a step on equality.
    :return: Every non-empty `if:` expression found, as strings.
    """
    job_pairs, step_pairs = index
    found = [cond for job_name, cond in job_pairs if job_name.startswith(name)]
    found.extend(cond for step_name, cond in step_pairs if step_name == name)
    return [cond for cond in found if cond]


def check_metadata_keys(ctx: LintContext) -> list[CheckResult]:
    """Check the metadata keys workflows request still exist.

    A downstream repository owns the job bodies of its header-only workflows:
    `repomatic init` syncs their `name`, `on` and `concurrency` blocks and the
    `uses:` pins, and never touches the steps below. So a key retired upstream
    keeps being asked for by a `run:` line nothing sweeps, and the
    `show-metadata` command answers a retired key with a `UsageError`. Since
    every other job in a test workflow reaches it through `needs:`, the whole
    run dies at the first job, on the next push, from a workflow file that
    looks freshly synced. A step still calling the `metadata` alias, which
    `8.0.0` removed, dies the same way.

    That is not hypothetical: `coverage_cells` went away with the Codecov
    integration and took a downstream test workflow down with it. Failing here
    instead moves the report to lint time, where it names the file and the job.
    """
    if not ctx.workflows:
        return [CheckResult(None, "Metadata keys check: skipped (no workflows).")]

    package = repo_name(DEFAULT_REPO)
    command = f"{package} {METADATA_SUBCOMMAND}"
    results: list[CheckResult] = []
    for request in unknown_metadata_keys(ctx.workflows, package):
        where = f"{request.path.name}:{request.job_id}"
        if request.removed_alias:
            results.append(
                CheckResult(
                    False,
                    f"{where} calls `{package} metadata`, which `8.0.0` removed,"
                    f" so this job fails on its next run, and every job gated on"
                    f" it through `needs:` with it. Call `{command}` instead.",
                )
            )
        elif request.unknown:
            names = ", ".join(f"`{key}`" for key in request.unknown)
            results.append(
                CheckResult(
                    False,
                    f"{where} asks `{command}` for {names}, which no longer"
                    f" exists. The command rejects an unknown key outright, so"
                    f" this job fails on its next run, and every job gated on"
                    f" it through `needs:` with it. Run `{command} --list-keys`"
                    f" for the current set.",
                )
            )
        else:
            count = len(request.keys)
            plural = "" if count == 1 else "s"
            results.append(
                CheckResult(
                    True,
                    f"{where}: {count} requested metadata key{plural}"
                    f" exist{'s' if count == 1 else ''}.",
                )
            )

    if not results:
        return [CheckResult(None, f"Metadata keys check: no `{command}` calls.")]
    return results


def check_pr_templates(ctx: LintContext) -> list[CheckResult]:
    """Check a repository's own `pr-body --template-file` templates.

    A repo with a custom PR-opening job ships the body as a file of its own
    rather than adding a template upstream. Three failure modes are flagged:

    1. The file sits outside {data}`PR_TEMPLATE_DIR`.
    2. A workflow references a path that does not exist, which the job only
       discovers when it runs and `pr-body` rejects the missing file.
    3. The frontmatter lacks a `title`, or does not opt out of the attribution
       footer the way {func}`~repomatic.github.pr_body.footer_opted_out` reads
       it. An absent field, `'False'`, and every other value keep the footer,
       and the failure is silent: the rendered body carries it twice.

    A `docs` field is not required. It deep-links the hosted workflows
    reference, which documents upstream jobs only.
    """
    # Map each referenced path to the workflows naming it, so a misplaced
    # template is reported against the file someone has to edit. Keys stay
    # POSIX throughout: a workflow always spells the path with `/`, so the
    # glob below must too, or on Windows one template lands in `candidates`
    # twice and the backslash spelling loses its referencing workflow.
    referenced: dict[str, set[str]] = {}
    for wf, content in ctx.workflow_texts.items():
        for match in TEMPLATE_FILE_ARG_RE.finditer(content):
            arg_path = match["path"].strip("\"'")
            referenced.setdefault(arg_path, set()).add(wf.name)

    # Templates already in the canonical directory are checked even when no
    # workflow names them, so a body built by a script is covered too.
    candidates = set(referenced)
    if PR_TEMPLATE_DIR.is_dir():
        candidates.update(found.as_posix() for found in PR_TEMPLATE_DIR.glob("*.md"))

    if not candidates:
        return [
            CheckResult(None, "PR template check: no repository-local templates found.")
        ]

    results: list[CheckResult] = []
    for raw_path in sorted(candidates):
        path = Path(raw_path)
        callers = ", ".join(
            f"`{name}`" for name in sorted(referenced.get(raw_path, ()))
        )
        origin = f" (referenced by {callers})" if callers else ""
        failures: list[str] = []

        if path.parent != PR_TEMPLATE_DIR:
            failures.append(
                f"PR template `{raw_path}`{origin} lives outside"
                f" `{PR_TEMPLATE_DIR.as_posix()}/`. Move it there and drop any"
                f" `pr-` prefix from its name, so the basename can match its"
                f" job ID and PR branch."
            )

        if not path.is_file():
            failures.append(
                f"PR template `{raw_path}`{origin} does not exist. The job"
                f" fails when it runs."
            )
        else:
            meta, _body = split_frontmatter(path.read_text(encoding="UTF-8"))
            if not meta.get("title"):
                failures.append(
                    f"PR template `{raw_path}` has no `title` in its"
                    f" frontmatter, so the PR and its commit are left unnamed."
                )
            if "footer" not in meta:
                failures.append(
                    f"PR template `{raw_path}` declares no `footer` field, so"
                    f" it opts in to an attribution footer the metadata block"
                    f" already appends. Add `footer: false`."
                )
            elif not footer_opted_out(meta):
                failures.append(
                    f"PR template `{raw_path}` sets `footer:"
                    f" {meta['footer']!r}`, which keeps the attribution footer"
                    f" the metadata block already appends. Set `footer: false`."
                )

        if failures:
            results.extend(CheckResult(False, msg) for msg in failures)
        else:
            results.append(CheckResult(True, f"PR template `{raw_path}`: conforms."))

    return results
