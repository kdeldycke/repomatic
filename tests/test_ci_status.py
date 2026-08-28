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


def _gh(listing, jobs, catalog=()):
    """A `run_gh_command` double serving the three read shapes.

    A workflow catalog for `workflow list`, a run listing for `run list`
    (batched or per-workflow alike), and one job payload for `run view`.
    """

    def dispatch(args):
        if args[0] == "workflow":
            return json.dumps(list(catalog))
        if args[1] == "list":
            return json.dumps(listing)
        return json.dumps({"jobs": jobs})

    return dispatch


def test_latest_run_reads_jobs():
    """The run and its jobs come back as one object."""
    listing = [
        {
            "databaseId": 42,
            "workflowName": "🔬 Tests",
            "status": "queued",
            "conclusion": "",
            "headSha": "abc1234def",
        }
    ]
    jobs = [
        {
            "name": "✅ ubuntu-26.04 / py3.10",
            "status": "completed",
            "conclusion": "failure",
        },
        {"name": "⁉️ py3.15-dev", "status": "completed", "conclusion": "failure"},
    ]
    with patch("repomatic.github.gh.run_gh_command", side_effect=_gh(listing, jobs)):
        status = latest_run("tests.yaml", "main")
    assert status is not None
    assert status.run_id == 42
    assert len(status.failed_required) == 1
    assert len(status.failed_probes) == 1


def test_latest_run_with_no_run():
    """An empty listing is not an error: GitHub may not have materialized one."""
    with patch("repomatic.github.gh.run_gh_command", side_effect=_gh([], [])):
        assert latest_run("tests.yaml", "main") is None


def test_read_ci_status_skips_workflows_without_a_run():
    """A workflow with no run drops out rather than reporting a fake green."""
    with patch("repomatic.github.gh.run_gh_command", side_effect=_gh([], [])):
        status = read_ci_status(["tests.yaml", "lint.yaml"], "main")
    assert status.runs == []
    assert status.blocking == []
    assert status.settled is True


def test_read_ci_status_batches_the_branch_listing():
    """One branch-wide listing serves every workflow the window covers."""
    catalog = [
        {"id": 7, "path": ".github/workflows/tests.yaml"},
        {"id": 8, "path": ".github/workflows/lint.yaml"},
    ]
    listing = [
        {
            "databaseId": 42,
            "workflowDatabaseId": 7,
            "workflowName": "🔬 Tests",
            "status": "completed",
            "conclusion": "success",
            "headSha": "abc1234def",
        },
        # An older run of the same workflow, which the newest-first scan skips.
        {
            "databaseId": 41,
            "workflowDatabaseId": 7,
            "workflowName": "🔬 Tests",
            "status": "completed",
            "conclusion": "failure",
            "headSha": "0ld4sha00",
        },
        {
            "databaseId": 40,
            "workflowDatabaseId": 8,
            "workflowName": "Lint",
            "status": "completed",
            "conclusion": "success",
            "headSha": "abc1234def",
        },
    ]
    jobs = [
        {
            "name": "✅ ubuntu-26.04 / py3.10",
            "status": "completed",
            "conclusion": "success",
        }
    ]
    with patch(
        "repomatic.github.gh.run_gh_command", side_effect=_gh(listing, jobs, catalog)
    ) as mock:
        status = read_ci_status(["tests.yaml", "lint.yaml"], "main")
    assert [run.run_id for run in status.runs] == [42, 40]
    assert [run.workflow for run in status.runs] == ["🔬 Tests", "Lint"]
    # One workflow catalog, one branch listing, two job views: no
    # per-workflow listings at all.
    assert mock.call_count == 4


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
