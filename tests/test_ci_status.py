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

from repomatic.github.ci_status import (
    CIStatus,
    JobStatus,
    RunStatus,
    latest_run,
    monitored_workflows,
    read_ci_status,
    workflow_files,
)


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
    ),
)
def test_job_requirement_reads_the_glyph(name, required):
    """Classification keys off the glyph, never a positional field."""
    assert job(name).required is required


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
    """Only `failure` counts: a skipped job is a gate that did not apply."""
    status = run(job("✅ a", "skipped"), job("✅ b", "cancelled"))
    assert status.failed_required == ()
    assert status.verdict == "green"


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


def _run_entry(run_id, filename, branch="main"):
    """One entry of a per-commit run listing."""
    return {
        "id": run_id,
        "path": f".github/workflows/{filename}",
        "head_branch": branch,
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
