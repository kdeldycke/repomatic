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

"""Tests for :mod:`repomatic.github.workflow_files`."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from repomatic.github import workflow_files
from repomatic.github.workflow_files import (
    WORKFLOW_DIR,
    github_yaml_paths,
    iter_jobs,
    iter_steps,
    jobs_of,
    literal_runners,
    load_workflow,
    load_workflows,
    parse_workflow_texts,
    read_workflow_texts,
    steps_of,
    workflow_paths,
    workflow_triggers,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


def _write(path: Path, content: str) -> Path:
    """Write *content* at *path*, creating its parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="UTF-8")
    return path


def test_workflow_dir_is_the_one_github_reads():
    """The directory every reader walks is the one GitHub runs workflows from."""
    assert WORKFLOW_DIR.as_posix() == ".github/workflows"


def test_loader_prefers_libyaml():
    """The C loader parses when this PyYAML build ships it."""
    expected = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert workflow_files._YAML_LOADER is expected


def test_workflow_paths_reads_both_extensions(tmp_path):
    """GitHub reads `.yml` too, so a downstream repository may use it."""
    for name in ("zucchini.yaml", "apricot.yml", "notes.md", "banana.yaml"):
        _write(tmp_path / name, "on: push\n")
    (tmp_path / "plum.yaml").mkdir()
    assert [path.name for path in workflow_paths(tmp_path)] == [
        "apricot.yml",
        "banana.yaml",
        "zucchini.yaml",
    ]


def test_workflow_paths_of_a_missing_directory(tmp_path):
    """A repository with no workflows has nothing to walk."""
    assert workflow_paths(tmp_path / "missing") == []


def test_github_yaml_paths_covers_workflows_and_actions(tmp_path):
    """Composite actions carry pins too, at any depth under `.github/actions/`."""
    expected = [
        _write(tmp_path / ".github/actions/fig/action.yml", "runs: {}\n"),
        _write(tmp_path / ".github/actions/kiwi/deep/action.yaml", "runs: {}\n"),
        _write(tmp_path / ".github/workflows/ci.yaml", "on: push\n"),
        _write(tmp_path / ".github/workflows/lint.yml", "on: push\n"),
    ]
    _write(tmp_path / ".github/dependabot.yaml", "version: 2\n")
    assert github_yaml_paths(tmp_path) == sorted(expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        pytest.param("on: push\njobs: {}\n", {True: "push", "jobs": {}}, id="mapping"),
        pytest.param("- apricot\n- banana\n", None, id="list"),
        pytest.param("just a sentence\n", None, id="scalar"),
        pytest.param("", None, id="empty"),
        pytest.param("{{invalid", None, id="not-yaml"),
    ),
)
def test_load_workflow(text, expected):
    """Only a mapping is a workflow; anything else reads as `None`."""
    assert load_workflow(text) == expected


def test_parse_workflow_texts_warns_about_a_broken_file(tmp_path, caplog):
    """A file that does not parse is named in a warning, then skipped."""
    caplog.set_level(logging.WARNING)
    broken = tmp_path / "broken.yaml"
    texts = {
        tmp_path / "ok.yaml": "on: push\n",
        broken: "{{invalid",
        tmp_path / "list.yaml": "- apricot\n",
    }
    assert list(parse_workflow_texts(texts)) == [tmp_path / "ok.yaml"]
    assert f"Could not parse {broken}" in caplog.text


def test_load_workflows_reads_the_directory(tmp_path):
    """Texts and parses come from the same sorted walk."""
    _write(tmp_path / "b.yml", "on: push\njobs: {}\n")
    _write(tmp_path / "a.yaml", "on: pull_request\n")
    assert list(read_workflow_texts(tmp_path)) == [
        tmp_path / "a.yaml",
        tmp_path / "b.yml",
    ]
    workflows = load_workflows(tmp_path)
    assert list(workflows) == [tmp_path / "a.yaml", tmp_path / "b.yml"]
    assert workflows[tmp_path / "a.yaml"] == {True: "pull_request"}


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        pytest.param("on:\n  push:\n", {"push": None}, id="bare-on"),
        pytest.param('"on":\n  push:\n', {"push": None}, id="quoted-on"),
        pytest.param("on: push\n", {}, id="scalar-trigger"),
        pytest.param("name: fig\n", {}, id="no-trigger"),
    ),
)
def test_workflow_triggers(text, expected):
    """Both spellings of `on` read the same, and a non-mapping reads empty."""
    assert workflow_triggers(load_workflow(text)) == expected


def test_workflow_triggers_of_a_non_mapping():
    """A document that is not a mapping declares no trigger."""
    assert workflow_triggers(["apricot"]) == {}


@pytest.mark.parametrize(
    ("data", "expected"),
    (
        pytest.param(
            {"jobs": {"build": {"runs-on": "x"}, "broken": "apricot"}},
            ["build"],
            id="skips-a-malformed-job",
        ),
        pytest.param({"jobs": None}, [], id="null-jobs"),
        pytest.param({"jobs": ["build"]}, [], id="list-jobs"),
        pytest.param({}, [], id="no-jobs"),
    ),
)
def test_jobs_of(data, expected):
    """Only job mappings are walked, whatever else `jobs:` holds."""
    assert [job_id for job_id, _job in jobs_of(data)] == expected


@pytest.mark.parametrize(
    ("job", "expected"),
    (
        pytest.param(
            {"steps": [{"run": "echo fig"}, "apricot", None]},
            [{"run": "echo fig"}],
            id="skips-a-malformed-step",
        ),
        pytest.param({"steps": None}, [], id="null-steps"),
        pytest.param({"steps": {"run": "echo fig"}}, [], id="mapping-steps"),
        pytest.param({"uses": "./.github/workflows/build.yaml"}, [], id="no-steps"),
    ),
)
def test_steps_of(job, expected):
    """Only step mappings are walked, whatever else `steps:` holds."""
    assert list(steps_of(job)) == expected


def test_iter_jobs_and_steps_cross_every_workflow():
    """The walkers name the file and the job each item comes from."""
    workflows: dict[Path, dict[str, Any]] = {
        Path("ci.yaml"): {
            "jobs": {
                "test": {"steps": [{"run": "pytest"}, {"uses": "actions/checkout"}]},
                "call": {"uses": "./.github/workflows/build.yaml"},
            }
        },
        Path("lint.yaml"): {"jobs": {"lint": {"steps": [{"run": "ruff check"}]}}},
    }
    assert [(path.name, job_id) for path, job_id, _job in iter_jobs(workflows)] == [
        ("ci.yaml", "test"),
        ("ci.yaml", "call"),
        ("lint.yaml", "lint"),
    ]
    assert [
        (path.name, job_id, step) for path, job_id, step in iter_steps(workflows)
    ] == [
        ("ci.yaml", "test", {"run": "pytest"}),
        ("ci.yaml", "test", {"uses": "actions/checkout"}),
        ("lint.yaml", "lint", {"run": "ruff check"}),
    ]


@pytest.mark.parametrize(
    ("runner", "expected"),
    (
        # The whole reason this is separate from KNOWN_RUNNERS: an off-axis image
        # is still what the job runs on, and is precisely the one no axis is
        # watching for a deprecation announcement.
        pytest.param(
            "ubuntu-22.04", {"ubuntu-22.04": ["ci.yaml:build"]}, id="off-axis"
        ),
        pytest.param("ubuntu-26.04", {"ubuntu-26.04": ["ci.yaml:build"]}, id="on-axis"),
        pytest.param("ubuntu-latest", {"ubuntu-latest": ["ci.yaml:build"]}, id="alias"),
        pytest.param("${{ matrix.os }}", {}, id="expression"),
    ),
)
def test_literal_runners(tmp_path, runner, expected):
    """Every literal `runs-on:` is reported, axis membership notwithstanding."""
    _write(
        tmp_path / "ci.yaml",
        f"on: push\njobs:\n  build:\n    runs-on: {runner}\n"
        "    steps:\n      - run: echo apricot\n",
    )
    assert literal_runners(tmp_path) == expected


def test_literal_runners_skips_thin_callers(tmp_path):
    """A delegating job contributes no runner: it names none of its own."""
    _write(
        tmp_path / "ci.yaml",
        "on: push\njobs:\n  build:\n    uses: ./.github/workflows/_build.yaml\n",
    )
    assert literal_runners(tmp_path) == {}


def test_literal_runners_groups_every_location(tmp_path):
    """One image named by several jobs reports all of them, sorted by file."""
    job = "    runs-on: ubuntu-26.04\n    steps:\n      - run: echo apricot\n"
    _write(tmp_path / "ci.yaml", f"on: push\njobs:\n  build:\n{job}  test:\n{job}")
    _write(tmp_path / "deploy.yml", f"on: push\njobs:\n  deploy:\n{job}")
    assert literal_runners(tmp_path) == {
        "ubuntu-26.04": ["ci.yaml:build", "ci.yaml:test", "deploy.yml:deploy"],
    }


def test_literal_runners_reads_the_working_directory_by_default(tmp_path, monkeypatch):
    """With no argument, the repository in the working directory is read."""
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / WORKFLOW_DIR / "ci.yaml",
        "on: push\njobs:\n  build:\n    runs-on: macos-26\n"
        "    steps:\n      - run: echo apricot\n",
    )
    assert literal_runners() == {"macos-26": ["ci.yaml:build"]}
