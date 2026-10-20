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

"""Tests for CI job triage."""

from __future__ import annotations

import json
from textwrap import dedent
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.github.ci_status import (
    STABLE_GLYPH,
    UNSTABLE_GLYPH,
    CIStatus,
    JobStatus,
    RunStatus,
    latest_run,
    monitored_workflows,
    read_ci_status,
    workflow_files,
)
from tests.conftest import WORKFLOWS_DIR, load_workflow


def job(name: str, conclusion: str = "success", status: str = "completed"):
    """Shorthand for a settled job."""
    return JobStatus(name=name, status=status, conclusion=conclusion)


def run(*jobs: JobStatus, conclusion: str = "success", status: str = "completed"):
    """Shorthand for a run carrying *jobs*."""
    return RunStatus(
        workflow="🔬 Tests",
        run_id=1,
        head_sha="abc1234def",
        status=status,
        conclusion=conclusion,
        jobs=jobs,
    )


UNEXPANDED_MATRIX_JOB = (
    "${{ matrix.state == 'stable' && '✅' || '⁉️' }} ${{ matrix.os }} /"
    " py${{ matrix.python-label || matrix.python-version }}"
)
"""The name of a matrix job GitHub never expanded: its `name:` expression."""


# -- Job classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "required"),
    (
        ("✅ ubuntu-26.04 / py3.10", True),
        ("⁉️ ubuntu-26.04 / py3.15-dev", False),
        # The release engine prefixes the workflow, so the glyph is not the
        # first token of the name. Splitting on " / " would misfile both.
        ("release / ✅ ubuntu-26.04, abc1234 build", True),
        ("release / ⁉️ windows-11-arm, abc1234 build", False),
        # A non-matrix job carries no glyph and is required.
        ("Lint types", True),
        ("Sync pull request", True),
        # A matrix GitHub never expanded keeps its `name:` expression, which
        # spells both glyphs. Some of its cells are required, so it is.
        pytest.param(UNEXPANDED_MATRIX_JOB, True, id="unexpanded matrix"),
        pytest.param(
            f"release / {UNEXPANDED_MATRIX_JOB}",
            True,
            id="unexpanded matrix behind a caller",
        ),
    ),
)
def test_job_requirement_reads_the_glyph(name, required):
    """Classification keys off the glyph, never a positional field."""
    assert job(name).required is required


def test_a_job_name_template_reads_as_required():
    """Every job name that picks its glyph from the matrix is required unexpanded.

    GitHub shows the `name:` expression as written for a matrix it never
    expanded. Each of those templates holds both glyphs. One that held the
    unstable glyph alone would read as a probe, and hide the required cells
    that never ran.
    """
    templates = [
        (path.name, job_id, str(definition.get("name") or ""))
        for path in sorted(WORKFLOWS_DIR.glob("*.yaml"))
        for job_id, definition in load_workflow(path.name).get("jobs", {}).items()
    ]
    stability_templates = [
        (filename, job_id, name)
        for filename, job_id, name in templates
        if "${{" in name and (STABLE_GLYPH in name or UNSTABLE_GLYPH in name)
    ]
    # An empty population would make the loop below prove nothing.
    assert stability_templates
    for filename, job_id, name in stability_templates:
        assert job(name).required, (
            f"{filename}: an unexpanded {job_id!r} job reads as a probe. Its name"
            f" template must hold both stability glyphs: {name!r}"
        )


def test_a_matrix_never_expanded_is_required():
    """A run stopped before its matrix got its cells left required cells undone.

    One cancelled job stands for the whole matrix, and every other job passed.
    Reading that job as a probe makes the run green.
    """
    status = run(
        job("🧬 Project metadata"),
        job(UNEXPANDED_MATRIX_JOB, "cancelled"),
        conclusion="cancelled",
    )
    assert status.verdict == "cancelled (1 required job(s))"
    assert status.inconclusive is True


def test_a_failed_matrix_never_expanded_blocks():
    """A matrix that failed before it expanded is a required failure."""
    status = run(job(UNEXPANDED_MATRIX_JOB, "failure"), conclusion="failure")
    assert status.failed_probes == ()
    assert status.blocking is True
    assert status.verdict == "1 required job(s) failed"


def test_probe_failure_does_not_block():
    """An allowed-failure cell is reported and never gates."""
    status = run(job("✅ ubuntu-26.04 / py3.10"), job("⁉️ py3.15-dev", "failure"))
    assert status.failed_required == ()
    assert len(status.failed_probes) == 1
    assert status.blocking is False
    assert "1 probe(s) failed" in status.verdict


def test_required_failure_blocks():
    """A red required cell holds up the merge."""
    status = run(job("✅ ubuntu-26.04 / py3.10", "failure"), conclusion="failure")
    assert status.blocking is True
    assert "1 required job(s) failed" in status.verdict


def test_a_probe_failure_inside_a_green_run_is_still_surfaced():
    """`continue-on-error` folds a crash into a green run conclusion.

    This is why the run's own conclusion never answers "is anything broken".
    """
    status = run(job("⁉️ py3.15-dev", "failure"), conclusion="success")
    assert status.conclusion == "success"
    assert len(status.failed_probes) == 1


def test_queued_run_status_does_not_hide_finished_jobs():
    """A run reads `queued` while its jobs are already settling."""
    status = run(
        job("✅ ubuntu-26.04 / py3.10", "failure"),
        job("✅ macos-26 / py3.10", "", "in_progress"),
        status="queued",
        conclusion="",
    )
    assert status.status == "queued"
    assert status.blocking is True
    assert len(status.running_jobs) == 1


def test_workflow_level_failure_has_no_failed_job():
    """A run failing around its jobs is a workflow error, not a benign one."""
    status = run(job("✅ ubuntu-26.04 / py3.10"), conclusion="failure")
    assert status.workflow_level_failure is True
    assert status.blocking is True
    assert status.verdict == "workflow-level failure"


def test_a_normal_failure_is_not_read_as_workflow_level():
    """With a failed job to point at, the run failed inside a job."""
    status = run(job("Lint types", "failure"), conclusion="failure")
    assert status.workflow_level_failure is False


def test_skipped_and_cancelled_jobs_are_not_failures():
    """Only `failure` blocks: a skipped job is a gate that did not apply."""
    status = run(job("✅ a", "skipped"), job("✅ b", "cancelled"))
    assert status.failed_required == ()
    assert status.blocking is False


@pytest.mark.parametrize(
    ("jobs", "conclusion", "verdict", "inconclusive"),
    (
        # A skipped job beside jobs that ran is a gate that did not apply.
        ((("✅ a", "success"), ("✅ b", "skipped")), "success", "green", False),
        # A cancelled probe gates nothing.
        ((("✅ a", "success"), ("⁉️ b", "cancelled")), "cancelled", "green", False),
        # Every job skipped, as `tests.yaml` does for a version-bump commit.
        (
            (("🧬 Project metadata", "skipped"), ("✅ a", "skipped")),
            "skipped",
            "skipped (no job ran)",
            True,
        ),
        # A newer push stopped the run of the commit under it.
        (
            (("✅ a", "cancelled"), ("✅ b", "success"), ("⁉️ c", "cancelled")),
            "cancelled",
            "cancelled (1 required job(s))",
            True,
        ),
        # Stopped, or skipped, before GitHub created a job.
        ((), "cancelled", "cancelled (no job ran)", True),
        ((), "skipped", "skipped (no job ran)", True),
    ),
)
def test_a_run_without_a_result_never_reads_green(
    jobs, conclusion, verdict, inconclusive
):
    """A run that was cancelled, or that ran no job, says nothing about its commit.

    It is not a failure, so it never blocks. It is not a pass either: a `green`
    verdict there hides that the matrix never ran on the newest commits.
    """
    status = run(*(job(name, outcome) for name, outcome in jobs), conclusion=conclusion)
    assert status.verdict == verdict
    assert status.inconclusive is inconclusive
    assert status.blocking is False


def test_a_failure_outranks_a_cancellation():
    """A cancelled run that already holds a red required job is a red run."""
    status = run(
        job("✅ a", "failure"), job("✅ b", "cancelled"), conclusion="cancelled"
    )
    assert status.verdict == "1 required job(s) failed"
    assert status.blocking is True
    assert status.inconclusive is False


def test_a_run_still_going_is_not_inconclusive_yet():
    """Cancelled jobs beside running ones belong to a run that has not settled."""
    status = run(
        job("✅ a", "cancelled"),
        job("✅ b", "", "in_progress"),
        status="in_progress",
        conclusion="",
    )
    assert status.verdict == "1 job(s) still running"
    assert status.inconclusive is False


# -- Workflow discovery -------------------------------------------------------


def test_monitored_workflows_selects_push_triggered_files(tmp_path):
    """Derived from the tree, so a new workflow is watched automatically."""
    (tmp_path / "tests.yaml").write_text(
        dedent("""\
            name: Tests
            on:
              push:
                branches: [main]
            """),
        encoding="UTF-8",
    )
    (tmp_path / "_engine.yaml").write_text(
        "name: Engine\non:\n  workflow_call:\n", encoding="UTF-8"
    )
    (tmp_path / "nightly.yaml").write_text(
        "name: Nightly\non:\n  schedule:\n    - cron: '0 0 * * *'\n", encoding="UTF-8"
    )
    assert monitored_workflows(tmp_path) == ["tests.yaml"]


def test_monitored_workflows_reads_the_yaml_boolean_on_key(tmp_path):
    """A bare `on:` parses as the boolean `True` under YAML 1.1."""
    (tmp_path / "lint.yaml").write_text(
        "name: Lint\non:\n  push: null\n", encoding="UTF-8"
    )
    assert monitored_workflows(tmp_path) == ["lint.yaml"]


def test_monitored_workflows_missing_directory(tmp_path):
    """A repo with no workflows reports none rather than raising."""
    assert monitored_workflows(tmp_path / "absent") == []


def test_monitored_workflows_skips_unparsable_file(tmp_path):
    """One malformed file never aborts the sweep."""
    (tmp_path / "broken.yaml").write_text("name: [unclosed\n", encoding="UTF-8")
    (tmp_path / "ok.yaml").write_text("name: Ok\non:\n  push:\n", encoding="UTF-8")
    assert monitored_workflows(tmp_path) == ["ok.yaml"]


# -- Reading runs -------------------------------------------------------------


TIP_SHA = "a" * 40
PARENT_SHA = "b" * 40
STALE_SHA = "c" * 40

GREEN_JOB = {
    "name": "✅ ubuntu-26.04 / py3.10",
    "status": "completed",
    "conclusion": "success",
}
RED_JOB = {
    "name": "✅ ubuntu-26.04 / py3.10",
    "status": "completed",
    "conclusion": "failure",
}
CANCELLED_JOB = {
    "name": "✅ ubuntu-26.04 / py3.10",
    "status": "completed",
    "conclusion": "cancelled",
}
SKIPPED_JOB = {
    "name": "🧬 Project metadata",
    "status": "completed",
    "conclusion": "skipped",
}


def _run_entry(run_id, filename, branch="main", conclusion="success"):
    """One entry of a per-commit run listing."""
    return {
        "id": run_id,
        "path": f".github/workflows/{filename}",
        "head_branch": branch,
        "conclusion": conclusion,
    }


def _detail(sha, *jobs, conclusion="success", status="completed"):
    """The `run view` payload of one run."""
    return {
        "workflowName": "🔬 Tests",
        "headSha": sha,
        "status": status,
        "conclusion": conclusion,
        "jobs": list(jobs),
    }


def _gh(history=(), runs_by_sha=None, listing=(), details=None, catalog=()):
    """A `run_gh_command` double serving every read shape.

    The commit list answers from *history*, a per-commit run listing from
    *runs_by_sha*, a workflow's run listing (the endpoint and `gh run list`
    alike) from *listing*, a `run view` from *details* by run ID, and a
    workflow list from *catalog*.
    """
    runs_by_sha = runs_by_sha or {}
    details = details or {}

    def dispatch(args):
        if args[0] == "api":
            if "/commits?" in args[1]:
                return json.dumps([{"sha": sha} for sha in history])
            if "/actions/workflows/" in args[1]:
                return json.dumps({"workflow_runs": list(listing)})
            sha = args[1].partition("head_sha=")[2].partition("&")[0]
            return json.dumps({"workflow_runs": runs_by_sha.get(sha, [])})
        if args[0] == "workflow":
            return json.dumps(list(catalog))
        if args[1] == "list":
            return json.dumps(list(listing))
        return json.dumps(details[int(args[2])])

    return dispatch


def _called(mock, *words):
    """Whether any `gh` command line holds every one of *words*."""
    return any(
        all(word in " ".join(call.args[0]) for word in words)
        for call in mock.call_args_list
    )


def test_latest_run_reads_jobs():
    """The run and its jobs come back as one object."""
    details = {
        42: _detail(
            TIP_SHA,
            RED_JOB,
            {"name": "⁉️ py3.15-dev", "status": "completed", "conclusion": "failure"},
            status="queued",
            conclusion="",
        )
    }
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh(listing=[{"id": 42}], details=details),
    ):
        status = latest_run("tests.yaml", "main")
    assert status is not None
    assert status.run_id == 42
    assert status.head_sha == TIP_SHA
    assert len(status.failed_required) == 1
    assert len(status.failed_probes) == 1


def test_latest_run_with_no_run():
    """An empty listing is not an error: GitHub may not have materialized one."""
    with patch("repomatic.github.gh.run_gh_command", side_effect=_gh()):
        assert latest_run("tests.yaml", "main") is None


def test_an_unreadable_run_fails_rather_than_reading_green():
    """A payload that is not a run raises instead of producing a jobless run."""
    with (
        patch(
            "repomatic.github.gh.run_gh_command",
            side_effect=_gh(listing=[{"id": 42}], details={42: None}),
        ),
        pytest.raises(TypeError, match="run 42"),
    ):
        latest_run("tests.yaml", "main")


def test_read_ci_status_skips_workflows_without_a_run():
    """A workflow with no run drops out rather than reporting a fake green."""
    with patch("repomatic.github.gh.run_gh_command", side_effect=_gh()):
        status = read_ci_status(["tests.yaml", "lint.yaml"], "main")
    assert status.runs == []
    assert status.tip_sha == ""
    assert status.blocking == []
    assert status.settled is True


def test_read_ci_status_anchors_on_the_tip():
    """Every workflow that ran on the tip is read there, and the walk stops."""
    runs_by_sha = {
        TIP_SHA: [
            # A tag pushed on the same commit: not a run of the branch.
            _run_entry(43, "tests.yaml", branch="v1.2.3"),
            _run_entry(42, "tests.yaml"),
            # An older run of the same workflow, which the newest-first scan skips.
            _run_entry(41, "tests.yaml"),
            _run_entry(40, "lint.yaml"),
        ]
    }
    details = {run_id: _detail(TIP_SHA, GREEN_JOB) for run_id in (40, 41, 42, 43)}
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh([TIP_SHA, PARENT_SHA], runs_by_sha, details=details),
    ) as mock:
        status = read_ci_status(["tests.yaml", "lint.yaml"], "main")
    assert [run.run_id for run in status.runs] == [42, 40]
    assert status.tip_sha == TIP_SHA
    assert status.runs_on_tip == status.runs
    # One commit list, one run listing for the tip, two run views: the parent
    # is never read, and the branch listing is never consulted.
    assert mock.call_count == 4
    assert not _called(mock, PARENT_SHA)
    assert not _called(mock, "list")


def test_read_ci_status_walks_back_to_a_path_filtered_workflow():
    """A workflow the tip skipped is read on the commit that last started it."""
    runs_by_sha = {
        TIP_SHA: [_run_entry(42, "tests.yaml")],
        # The parent's own run of `tests.yaml` is older than the tip's.
        PARENT_SHA: [_run_entry(31, "tests.yaml"), _run_entry(30, "docs.yaml")],
    }
    details = {
        42: _detail(TIP_SHA, GREEN_JOB),
        31: _detail(PARENT_SHA, RED_JOB, conclusion="failure"),
        30: _detail(PARENT_SHA, GREEN_JOB),
    }
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh([TIP_SHA, PARENT_SHA], runs_by_sha, details=details),
    ):
        status = read_ci_status(["tests.yaml", "docs.yaml"], "main")
    assert [(run.run_id, run.head_sha) for run in status.runs] == [
        (42, TIP_SHA),
        (30, PARENT_SHA),
    ]
    assert [run.run_id for run in status.runs_on_tip] == [42]
    assert status.blocking == []


def test_read_ci_status_walks_back_from_a_run_that_ran_no_job():
    """A workflow that skipped every job on the tip is read where it last ran.

    The tip is a version-bump commit: `tests.yaml` skips it, and its push
    cancelled the run of the commit under it. The skipped run reads as a
    finished one with no failure, which hides that the matrix never ran on the
    change.
    """
    runs_by_sha = {
        TIP_SHA: [
            _run_entry(42, "tests.yaml", conclusion="skipped"),
            _run_entry(40, "docs.yaml"),
        ],
        PARENT_SHA: [_run_entry(31, "tests.yaml", conclusion="cancelled")],
    }
    details = {
        42: _detail(TIP_SHA, SKIPPED_JOB, conclusion="skipped"),
        40: _detail(TIP_SHA, GREEN_JOB),
        31: _detail(PARENT_SHA, CANCELLED_JOB, conclusion="cancelled"),
    }
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh([TIP_SHA, PARENT_SHA], runs_by_sha, details=details),
    ):
        status = read_ci_status(["tests.yaml", "docs.yaml"], "main")
    assert [(run.run_id, run.head_sha) for run in status.runs] == [
        (31, PARENT_SHA),
        (40, TIP_SHA),
    ]
    assert status.skipped_on_tip == ["tests.yaml"]
    assert status.runs[0].verdict == "cancelled (1 required job(s))"
    assert [run.inconclusive for run in status.runs] == [True, False]
    assert status.blocking == []


def test_read_ci_status_stops_at_a_cancelled_run():
    """A cancelled run is the newest word on its workflow, whatever ran before.

    The green run of the parent is older than the commit left without a result:
    reporting it would read as a pass on a tree it never saw.
    """
    runs_by_sha = {
        TIP_SHA: [_run_entry(42, "tests.yaml", conclusion="cancelled")],
        PARENT_SHA: [_run_entry(31, "tests.yaml")],
    }
    details = {
        42: _detail(TIP_SHA, CANCELLED_JOB, conclusion="cancelled"),
        31: _detail(PARENT_SHA, GREEN_JOB),
    }
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh([TIP_SHA, PARENT_SHA], runs_by_sha, details=details),
    ) as mock:
        status = read_ci_status(["tests.yaml"], "main")
    assert [(run.run_id, run.head_sha) for run in status.runs] == [(42, TIP_SHA)]
    assert status.runs[0].inconclusive is True
    assert status.skipped_on_tip == []
    assert not _called(mock, PARENT_SHA)


def test_read_ci_status_reports_a_workflow_that_only_skipped():
    """With nothing but skipped runs in the window, the newest one is reported.

    Its own verdict says the tip has no result, so the tip note leaves it out.
    """
    runs_by_sha = {
        TIP_SHA: [_run_entry(42, "tests.yaml", conclusion="skipped")],
        PARENT_SHA: [_run_entry(31, "tests.yaml", conclusion="skipped")],
    }
    details = {
        42: _detail(TIP_SHA, SKIPPED_JOB, conclusion="skipped"),
        31: _detail(PARENT_SHA, SKIPPED_JOB, conclusion="skipped"),
    }
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh([TIP_SHA, PARENT_SHA], runs_by_sha, details=details),
    ) as mock:
        status = read_ci_status(["tests.yaml"], "main")
    assert [(run.run_id, run.head_sha) for run in status.runs] == [(42, TIP_SHA)]
    assert status.runs[0].verdict == "skipped (no job ran)"
    assert status.runs[0].inconclusive is True
    assert status.skipped_on_tip == []
    assert not _called(mock, "actions/workflows/")


@pytest.mark.parametrize(("with_result", "skipped"), ((44, 43), (43, 44)))
def test_read_ci_status_prefers_a_run_with_a_result_on_the_same_commit(
    with_result, skipped
):
    """Of two runs on one commit, the one that ran a job is read, newer or not.

    The usual case is a run dispatched on a commit whose push run skipped every
    job. The other order is a later event that no job of the workflow answers.
    """
    entries = {
        with_result: _run_entry(with_result, "tests.yaml"),
        skipped: _run_entry(skipped, "tests.yaml", conclusion="skipped"),
    }
    # Newest first, as the API lists them.
    runs_by_sha = {TIP_SHA: [entries[run_id] for run_id in sorted(entries)[::-1]]}
    details = {
        with_result: _detail(TIP_SHA, GREEN_JOB),
        skipped: _detail(TIP_SHA, SKIPPED_JOB, conclusion="skipped"),
    }
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh([TIP_SHA, PARENT_SHA], runs_by_sha, details=details),
    ) as mock:
        status = read_ci_status(["tests.yaml"], "main")
    assert [run.run_id for run in status.runs] == [with_result]
    assert status.skipped_on_tip == []
    assert status.runs[0].inconclusive is False
    assert not _called(mock, PARENT_SHA)


def test_read_ci_status_ignores_a_stale_branch_listing():
    """A months-old snapshot from the branch listing never reaches the report.

    The listing below claims the latest `tests.yaml` run failed on an old
    commit, while the tip's own run is green. A reader trusting the listing
    reports the old red.
    """
    stale_listing = [
        {
            "databaseId": 1,
            "workflowDatabaseId": 7,
            "workflowName": "🔬 Tests",
            "status": "completed",
            "conclusion": "failure",
            "headSha": STALE_SHA,
        }
    ]
    details = {
        1: _detail(STALE_SHA, RED_JOB, conclusion="failure"),
        42: _detail(TIP_SHA, GREEN_JOB),
    }
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh(
            [TIP_SHA],
            {TIP_SHA: [_run_entry(42, "tests.yaml")]},
            listing=stale_listing,
            details=details,
            catalog=[{"id": 7, "path": ".github/workflows/tests.yaml"}],
        ),
    ):
        status = read_ci_status(["tests.yaml"], "main")
    assert [run.head_sha for run in status.runs] == [TIP_SHA]
    assert status.blocking == []


def test_read_ci_status_falls_back_past_the_window():
    """A workflow with no run on the newest commits asks its dated run listing."""
    details = {7: _detail(STALE_SHA, GREEN_JOB)}
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=_gh([TIP_SHA], {TIP_SHA: []}, listing=[{"id": 7}], details=details),
    ) as mock:
        status = read_ci_status(["nightly.yaml"], "main")
    assert [run.run_id for run in status.runs] == [7]
    assert status.runs_on_tip == []
    assert _called(mock, "actions/workflows/nightly.yaml/runs", "created=%3E%3D")
    assert not _called(mock, "run list")


def test_runs_on_tip_is_empty_before_the_tip_runs():
    """Right after a push, every run read belongs to an earlier commit."""
    earlier = RunStatus(
        workflow="🔬 Tests",
        run_id=1,
        head_sha=PARENT_SHA,
        status="completed",
        conclusion="success",
    )
    status = CIStatus(branch="main", tip_sha=TIP_SHA, runs=[earlier])
    assert status.runs_on_tip == []
    assert status.settled is True


def test_ci_status_command_reports_a_run_without_a_result():
    """The command says which commit has no result, and still exits clean.

    Nothing failed, so nothing is fatal: the report is what tells a cancelled
    or skipped run from a green one.
    """
    cancelled = RunStatus(
        workflow="🔬 Tests",
        run_id=31,
        head_sha=PARENT_SHA,
        status="completed",
        conclusion="cancelled",
        jobs=(job("✅ ubuntu-26.04 / py3.10", "cancelled"),),
    )
    green = RunStatus(
        workflow="📚 Docs",
        run_id=40,
        head_sha=TIP_SHA,
        status="completed",
        conclusion="success",
        jobs=(job("Deploy docs"),),
    )
    skipped = RunStatus(
        workflow="🧹 Lint",
        run_id=50,
        head_sha=TIP_SHA,
        status="completed",
        conclusion="skipped",
        jobs=(job("🛡️ Lint types", "skipped"),),
    )
    status = CIStatus(
        branch="main",
        tip_sha=TIP_SHA,
        runs=[cancelled, green, skipped],
        skipped_on_tip=["tests.yaml"],
    )
    with (
        patch(
            "repomatic.cli.ci.monitored_workflows",
            return_value=["tests.yaml", "docs.yaml", "lint.yaml"],
        ),
        patch("repomatic.cli.ci.read_ci_status", return_value=status),
    ):
        result = CliRunner().invoke(repomatic, ["ci-status"])

    assert result.exit_code == 0, result.output
    assert "cancelled (1 required job(s))" in result.output
    assert "skipped (no job ran)" in result.output
    assert (
        "⚠ 🔬 Tests: run 31 was cancelled before its required jobs finished, so"
        " bbbbbbbb has no result from it." in result.output
    )
    assert (
        "⚠ 🧹 Lint: run 50 ran no job, so aaaaaaaa has no result from it."
        in result.output
    )
    assert (
        "… no job ran on the tip of 'main' (aaaaaaaa) for: tests.yaml. Each one"
        " is read on an earlier commit." in result.output
    )
    assert "📚 Docs: run 40" not in result.output


def test_workflow_files_keeps_everything_with_runs_of_its_own(tmp_path):
    """Every trigger counts except `workflow_call`, which creates no run.

    `monitored_workflows` answers "what does a push start", which is a
    narrower question than "what may I name": a schedule-only workflow has
    runs worth reading, and a reusable one never does.
    """
    (tmp_path / "tests.yaml").write_text(
        "name: Tests\non:\n  push:\n    branches: [main]\n", encoding="UTF-8"
    )
    (tmp_path / "_engine.yaml").write_text(
        "name: Engine\non:\n  workflow_call:\n", encoding="UTF-8"
    )
    (tmp_path / "nightly.yaml").write_text(
        "name: Nightly\non:\n  schedule:\n    - cron: '0 0 * * *'\n", encoding="UTF-8"
    )
    assert workflow_files(tmp_path) == ("nightly.yaml", "tests.yaml")


def test_workflow_files_missing_directory(tmp_path):
    """A repository with no workflow directory offers no workflow."""
    assert workflow_files(tmp_path / "absent") == ()
