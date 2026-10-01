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

"""Which CI jobs are red, and which of those actually gate a merge.

repomatic names the jobs it generates, prefixing each matrix cell with a
glyph that records whether the cell is allowed to fail: `✅` for a required
one, `⁉️` for a probe running under `continue-on-error`. Reading that back
was left to whoever was watching CI, and it is easy to get wrong in three
specific ways this module exists to settle:

- **The glyph, not the position.** Job names differ in shape across
  workflows: `tests.yaml` emits `✅ ubuntu-26.04 / py3.10` while the release
  engine emits `workflow / ✅ ubuntu-26.04, abc1234 build`. Anything that
  splits on `" / "` and reads a fixed field strips the glyph off one of the
  two and files a required red as a probe, which reads as green.
- **Jobs, not the run.** A run's own `conclusion` is `success` while a
  `continue-on-error` probe inside it crashed, and its `status` still reads
  `queued` while a dozen of its jobs have already finished. Neither answers
  "is anything broken".
- **A run that failed around its jobs.** A `failure` conclusion with no
  failed job is a workflow-level error: an invalid `strategy.matrix`
  expression, malformed YAML, a missing secret. There is no job log to read,
  and treating it as benign is how a persistently red workflow gets written
  off as a known artifact.

A job carrying no stability glyph is required. That covers every non-matrix job
(`1️⃣ Run-once tests`, `📦 Package install`, `🛡️ Lint types`), where the absence
of a marker means the job was never optional rather than that its status is
unknown. Only the two stability glyphs count: a job name may carry any other
emoji and still be required, which is why the test is for `⁉️` specifically
rather than for a decorated name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import quote

import yaml
from click_extra import ColumnSpec

from .actions import workflow_runs
from .gh import gh_api_json
from .workflow_sync import workflow_triggers

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

TIP_HISTORY_DEPTH = 10
"""Commits {func}`read_ci_status` walks back from the branch tip, tip included.

A push starts most workflows on the tip itself. A workflow whose `paths:` filter
skipped the tip last ran on an earlier commit, which a walk this deep reaches on
any active branch. A workflow with no run in the window falls back to
{func}`latest_run`.
"""

STABLE_GLYPH = "✅"
"""Marks a matrix cell that must pass. See {data}`UNSTABLE_GLYPH`."""

UNSTABLE_GLYPH = "⁉️"
"""Marks a matrix cell running under `continue-on-error`.

A red one never gates a merge, which is exactly why it has to be told apart
from a required cell rather than counted with it. A release still fixes what
it can: see the `repomatic-ship` skill on the genuinely-green goal.
"""

TERMINAL_STATUSES = frozenset({"completed"})
"""Job statuses meaning the job will not change again."""

CI_STATUS_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("workflow", "Workflow"),
    ColumnSpec("commit", "Commit"),
    ColumnSpec("run-status", "Run status"),
    ColumnSpec("verdict", "Verdict"),
)
"""Column definitions for the `ci-status` table."""


@dataclass(frozen=True)
class JobStatus:
    """One job of one workflow run."""

    name: str
    """The job's name, glyph included."""

    status: str
    """`queued`, `in_progress` or `completed`."""

    conclusion: str
    """`success`, `failure`, `cancelled`, `skipped`, or empty while running."""

    @property
    def required(self) -> bool:
        """Whether a failure here gates a merge.

        Looks for the glyph anywhere in the raw name rather than at its
        start. The two shapes disagree on where it sits: `tests.yaml` leads
        with it (`⁉️ ubuntu-26.04 / py3.15-dev`) while the release engine
        prefixes the workflow first (`release / ⁉️ windows-11-arm, abc1234
        build`). A leading-position test passes the first and silently files
        the second as required; splitting on `" / "` gets it wrong the other
        way round. Containment is the one form both satisfy, and the
        templates emit exactly one glyph per name.
        """
        return UNSTABLE_GLYPH not in self.name

    @property
    def failed(self) -> bool:
        """Whether this job reached a failing conclusion."""
        return self.conclusion == "failure"

    @property
    def running(self) -> bool:
        """Whether this job has yet to reach a terminal state."""
        return self.status not in TERMINAL_STATUSES


@dataclass(frozen=True)
class RunStatus:
    """The latest run of one workflow on one branch."""

    workflow: str
    """Workflow name, as GitHub reports it."""

    run_id: int
    """Numeric run ID, for `gh run view`."""

    head_sha: str
    """Commit the run was created for."""

    status: str
    """The run's own status. Lags its jobs, so it never gates anything here."""

    conclusion: str
    """The run's own conclusion, empty while it is still going."""

    jobs: tuple[JobStatus, ...] = ()
    """Every job of the run."""

    @property
    def failed_required(self) -> tuple[JobStatus, ...]:
        """Failing jobs that gate a merge."""
        return tuple(job for job in self.jobs if job.failed and job.required)

    @property
    def failed_probes(self) -> tuple[JobStatus, ...]:
        """Failing jobs allowed to fail."""
        return tuple(job for job in self.jobs if job.failed and not job.required)

    @property
    def running_jobs(self) -> tuple[JobStatus, ...]:
        """Jobs that have not settled yet."""
        return tuple(job for job in self.jobs if job.running)

    @property
    def workflow_level_failure(self) -> bool:
        """Whether the run failed around its jobs rather than inside one.

        No job log explains this one: read the run's error annotations and
        fix the workflow itself.
        """
        return self.conclusion == "failure" and not any(job.failed for job in self.jobs)

    @property
    def blocking(self) -> bool:
        """Whether this run holds up a merge."""
        return bool(self.failed_required) or self.workflow_level_failure

    @property
    def verdict(self) -> str:
        """One-phrase outcome, for the table's last column."""
        if self.workflow_level_failure:
            return "workflow-level failure"
        if self.failed_required:
            return f"{len(self.failed_required)} required job(s) failed"
        if self.running_jobs:
            return f"{len(self.running_jobs)} job(s) still running"
        if self.failed_probes:
            return f"green ({len(self.failed_probes)} probe(s) failed)"
        return "green"


@dataclass
class CIStatus:
    """Every monitored workflow's latest run on a branch."""

    branch: str
    """Branch the runs were read from."""

    tip_sha: str = ""
    """Commit at the tip of the branch when the runs were read.

    Empty when the branch history could not be read.
    """

    runs: list[RunStatus] = field(default_factory=list)
    """One entry per workflow that has a run, newest first."""

    @property
    def blocking(self) -> list[RunStatus]:
        """Runs holding up a merge."""
        return [run for run in self.runs if run.blocking]

    @property
    def runs_on_tip(self) -> list[RunStatus]:
        """Runs created for the tip commit itself.

        Empty right after a push, while GitHub has yet to create the tip's runs:
        every run listed then belongs to an earlier commit.
        """
        return [run for run in self.runs if run.head_sha == self.tip_sha]

    @property
    def settled(self) -> bool:
        """Whether every run reached a terminal state."""
        return all(not run.running_jobs for run in self.runs)


def _workflow_triggers(workflow_dir: Path) -> list[tuple[str, set[str]]]:
    """Pair each workflow file in the directory with the triggers it declares.

    Both public readers below select on those triggers, and neither should
    parse the tree twice or disagree on which files count as workflows.

    :param workflow_dir: Directory holding the workflow files.
    :return: `(filename, triggers)` pairs, sorted by filename. Empty when the
        directory is missing. A file that does not parse is skipped.
    """
    if not workflow_dir.is_dir():
        return []
    pairs = []
    for path in sorted(workflow_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="UTF-8"))
        except yaml.YAMLError:
            logging.warning(f"Could not parse {path}, skipping.")
            continue
        pairs.append((path.name, set(workflow_triggers(data))))
    return pairs


def workflow_files(workflow_dir: Path) -> tuple[str, ...]:
    """Every workflow in the directory that has runs of its own.

    {func}`monitored_workflows` narrows this to the ones a push starts, which
    is the right default for a status report and the wrong set to *accept*: a
    schedule-only or dispatch-only workflow has runs worth reading too. A
    reusable workflow is excluded for the reason it is there, since its jobs
    only ever appear under a caller's run.

    :param workflow_dir: Directory holding the workflow files.
    :return: Workflow filenames, sorted.
    """
    return tuple(
        name
        for name, triggers in _workflow_triggers(workflow_dir)
        if triggers - {"workflow_call"}
    )


def monitored_workflows(workflow_dir: Path) -> list[str]:
    """Every workflow a push to the default branch can start.

    Derived from the tree rather than listed by hand, so a workflow added
    later is watched without anyone remembering to add it here. A reusable
    workflow is excluded: it has no runs of its own, only the ones its
    callers create.

    :param workflow_dir: Directory holding the workflow files.
    :return: Workflow filenames, sorted.
    """
    return [
        name
        for name, triggers in _workflow_triggers(workflow_dir)
        if "push" in triggers
    ]


def _run_status(run_id: int, workflow: str) -> RunStatus:
    """Read one run and its jobs by ID.

    Every field comes from the run itself rather than from the listing that
    found it, so the report never mixes a listing's view of a run with a
    fresher view of its jobs.

    :param run_id: The run's numeric ID.
    :param workflow: Workflow filename, the fallback display name.
    :return: The run with its jobs attached.
    :raises TypeError: When `gh` answers with something other than a run. A
        run with no status and no jobs would otherwise read as green.
    """
    detail = gh_api_json(
        [
            "run",
            "view",
            str(run_id),
            "--json",
            "conclusion,headSha,jobs,status,workflowName",
        ],
        strict=True,
    )
    if not isinstance(detail, dict):
        msg = f"Could not read run {run_id} of {workflow}."
        raise TypeError(msg)
    return RunStatus(
        workflow=detail.get("workflowName") or workflow,
        run_id=run_id,
        head_sha=detail.get("headSha") or "",
        status=detail.get("status") or "",
        conclusion=detail.get("conclusion") or "",
        jobs=tuple(
            JobStatus(
                name=job.get("name") or "",
                status=job.get("status") or "",
                conclusion=job.get("conclusion") or "",
            )
            for job in detail.get("jobs") or []
        ),
    )


def latest_run(workflow: str, branch: str) -> RunStatus | None:
    """Read a workflow's most recent run on *branch*, jobs included.

    Lists the workflow's runs through {func}`~repomatic.github.actions.workflow_runs`,
    which sends a `created` filter to keep a stale snapshot out.
    {func}`read_ci_status` asks it only about a workflow with no run on the
    newest commits.

    :param workflow: Workflow filename, like `tests.yaml`.
    :param branch: Branch to read runs from.
    :return: The run, or `None` when the workflow has none in the listing
        window. An empty listing is not proof the workflow was filtered out:
        GitHub can sit on a push event for hours before materializing a run.
    """
    # `strict`: an unreachable `gh` must fail the read rather than report the
    # workflow as run-less, which `/babysit-ci` would read as a green hole.
    runs = workflow_runs(workflow, branch, strict=True)
    if not runs:
        return None
    return _run_status(int(runs[0]["id"]), workflow)


def _branch_history(branch: str) -> list[str]:
    """SHAs of the newest commits on *branch*, tip first.

    :param branch: Branch to read.
    :return: At most {data}`TIP_HISTORY_DEPTH` SHAs. Empty when the branch
        cannot be read.
    """
    endpoint = (
        f"repos/{{owner}}/{{repo}}/commits?sha={quote(branch, safe='')}"
        f"&per_page={TIP_HISTORY_DEPTH}"
    )
    commits = gh_api_json(["api", endpoint], strict=True)
    if not isinstance(commits, list):
        return []
    return [commit["sha"] for commit in commits if commit.get("sha")]


def _runs_on_commit(sha: str, branch: str) -> dict[str, int]:
    """The newest run of each workflow GitHub created for commit *sha*.

    :param sha: Full commit SHA.
    :param branch: Branch the runs must belong to. The same commit also carries
        runs for a tag pushed on it, or for a pull request branch.
    :return: Run ID by workflow filename.
    """
    payload = gh_api_json(
        ["api", f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={sha}&per_page=100"],
        strict=True,
    )
    runs: dict[str, int] = {}
    if not isinstance(payload, dict):
        return runs
    # Newest first, per the API's ordering: the first entry seen for a
    # workflow is its latest run on this commit.
    for entry in payload.get("workflow_runs") or []:
        if entry.get("head_branch") != branch:
            continue
        filename = str(entry.get("path") or "").rpartition("/")[2]
        if filename and filename not in runs:
            runs[filename] = int(entry["id"])
    return runs


def read_ci_status(workflows: Iterable[str], branch: str) -> CIStatus:
    """Read the latest run of each workflow on *branch*.

    Anchored on the branch tip: reads the runs of each of the newest commits,
    tip first, and stops as soon as every workflow has one. A workflow whose
    `paths:` filter skipped the tip is found on the commit that last started
    it. Only a workflow with no run in the whole window falls back to
    {func}`latest_run`.

    ```{warning}
    The branch-wide run listing can answer with a stale snapshot: runs from
    commits months old, presented as the latest ones. Reading runs commit by
    commit, from a commit list that comes from git data rather than from the
    run index, keeps that snapshot out. The fallback listing sends a `created`
    filter for the same reason.
    ```

    :param workflows: Workflow filenames to read.
    :param branch: Branch to read runs from.
    :return: The collected status.
    """
    status = CIStatus(branch=branch)
    names = list(workflows)
    if not names:
        return status
    history = _branch_history(branch)
    if history:
        status.tip_sha = history[0]
    wanted = set(names)
    found: dict[str, int] = {}
    for sha in history:
        for filename, run_id in _runs_on_commit(sha, branch).items():
            if filename in wanted and filename not in found:
                found[filename] = run_id
        if len(found) == len(wanted):
            break
    for workflow in names:
        run = (
            _run_status(found[workflow], workflow)
            if workflow in found
            else latest_run(workflow, branch)
        )
        if run is None:
            logging.info(f"No run found for {workflow} on {branch}.")
            continue
        status.runs.append(run)
    return status
