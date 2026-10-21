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

"""Tests for `repomatic.metadata.git`: commit ranges and changed files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from repomatic.git_ops import NULL_SHA, Commit, GitError, head_sha
from repomatic.github.event import WorkflowEvent
from repomatic.metadata.core import Metadata
from repomatic.metadata.git import commit_matrix, version_at
from tests.conftest import metadata_from_pyproject
from tests.test_git_ops import _run_git, _seed_commit, make_shallow_clone


@pytest.fixture(autouse=True)
def _scrub_workflow_event(monkeypatch):
    """Detach every test from the CI run's own workflow event."""
    for envvar in (
        "GITHUB_BASE_REF",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_HEAD_REF",
        "GITHUB_SHA",
    ):
        monkeypatch.delenv(envvar, raising=False)


@pytest.fixture
def repository(tmp_path, monkeypatch, hermetic_git):
    """A fresh repository, the current directory."""
    _run_git("init", "--initial-branch=main", str(tmp_path), cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _push_event(tmp_path, monkeypatch, before: str, after: str) -> None:
    """Point the metadata at a push event from *before* to *after*."""
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": before, "after": after}), encoding="UTF-8")
    monkeypatch.setattr("repomatic.metadata.env.is_github_ci", lambda: True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_SHA", after)


@pytest.fixture
def shallow_push(tmp_path, monkeypatch, hermetic_git):
    """A push of the last three commits of a remote, received as a depth-1 clone.

    :return: The SHAs of the remote's commits, oldest first.
    """
    clone, shas = make_shallow_clone(tmp_path)
    monkeypatch.chdir(clone)
    _push_event(tmp_path, monkeypatch, shas[1], shas[-1])
    return shas


@pytest.mark.parametrize("changed_files_first", (True, False))
def test_push_range_resolves_in_a_shallow_clone(shallow_push, changed_files_first):
    """Both properties resolve, whichever reads the range first.

    `changed_files` fetches the start of the range on its own, which leaves a
    second boundary in the clone. `new_commits` must still walk the whole
    range from there, or a release push loses its release commit.
    """
    metadata = Metadata()
    if changed_files_first:
        assert metadata.changed_files == ("fruits.txt",)
    assert metadata.new_commits is not None
    assert [commit.hash for commit in metadata.new_commits] == shallow_push[2:]
    assert metadata.changed_files == ("fruits.txt",)


def test_push_creating_a_branch_has_no_range_start(shallow_push, tmp_path, monkeypatch):
    """The null SHA starts no range: the changed files are unknown."""
    _push_event(tmp_path, monkeypatch, NULL_SHA, shallow_push[-1])

    metadata = Metadata()
    assert metadata.changed_files is None
    assert metadata.new_commits is not None
    assert [commit.hash for commit in metadata.new_commits] == [shallow_push[-1]]


def test_new_commits_degrades_when_git_fails(monkeypatch, caplog):
    """A git failure degrades new_commits to None with git's stderr surfaced,
    instead of crashing the whole `metadata` command.

    Reproduces the compiled-binary self-test running inside a manylinux container
    over a checkout git refuses as "dubious ownership".
    """
    monkeypatch.setattr(Metadata, "commit_range", ("a" * 40, "b" * 40))

    def reject(sha):
        raise GitError(
            128,
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            stderr="fatal: detected dubious ownership in repository",
        )

    monkeypatch.setattr("repomatic.metadata.git.fetch_commit", reject)
    with caplog.at_level(logging.WARNING):
        assert Metadata().new_commits is None
    assert "dubious ownership" in caplog.text


def test_changed_files_surfaces_git_stderr(monkeypatch, caplog):
    """A `git diff` failure degrades changed_files to None and logs git's stderr."""
    monkeypatch.setattr(Metadata, "commit_range", ("a" * 40, "b" * 40))
    monkeypatch.setattr("repomatic.metadata.git.fetch_commit", lambda sha: True)

    def reject(start, end):
        raise GitError(
            128,
            ["git", "diff", "--name-only", start, end],
            stderr="fatal: detected dubious ownership in repository",
        )

    monkeypatch.setattr("repomatic.metadata.git.diff_names", reject)
    with caplog.at_level(logging.WARNING):
        assert Metadata().changed_files is None
    assert "dubious ownership" in caplog.text


@pytest.mark.parametrize(
    ("changed", "zsh_changed", "workflows_changed"),
    (
        pytest.param((".github/workflows/lint.yaml",), False, True, id="workflow"),
        pytest.param(("scripts/setup.zsh",), True, False, id="zsh"),
        pytest.param(("readme.md",), False, False, id="neither"),
    ),
)
def test_changed_file_kinds(changed, zsh_changed, workflows_changed):
    """`git diff` names, as strings, match the inventory's `Path` objects."""
    metadata = Metadata()
    metadata.__dict__["changed_files"] = changed
    metadata.__dict__["files"] = SimpleNamespace(
        zsh_files=[Path("scripts/setup.zsh")],
        workflow_files=[Path(".github/workflows/lint.yaml")],
    )
    assert metadata.zsh_changed is zsh_changed
    assert metadata.workflows_changed is workflows_changed


def _prime_push(metadata: Metadata, changed: tuple[str, ...]) -> None:
    """Prime *metadata* as a push to `main` changing *changed*."""
    metadata.__dict__["event_type"] = WorkflowEvent.push
    metadata.__dict__["head_branch"] = "main"
    metadata.__dict__["head_commit_message"] = "Water the papaya trees"
    metadata.__dict__["changed_files"] = changed


@pytest.mark.parametrize(
    "workflow_file",
    [
        ".github/workflows/_release-engine.yaml",
        ".github/workflows/release.yaml",
    ],
)
def test_skip_binary_build_release_workflow_push(workflow_file):
    """A push that only touches a release-pipeline workflow must rebuild binaries.

    Those workflows define how binaries are compiled and self-tested, so any
    change there needs a full matrix revalidation.
    """
    metadata = Metadata()
    _prime_push(metadata, (workflow_file,))
    assert metadata.skip_binary_build is False


SRC_LAYOUT_PYPROJECT = """\
[project]
name = "papaya"
version = "1.0.0"

[project.scripts]
papaya = "papaya.__main__:main"

[tool.repomatic]
workflow.source-paths = ["src"]
"""
"""A project keeping its package under `src/`, which its entry point does not name."""


def test_binary_affecting_paths_include_source_paths(tmp_path, monkeypatch):
    """A source-only push to a `src/` layout still builds its binaries."""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, SRC_LAYOUT_PYPROJECT)
    assert {"papaya/", "src/"} <= set(metadata.binary_affecting_paths)

    _prime_push(metadata, ("src/papaya/cli.py",))
    assert metadata.skip_binary_build is False


def _commit_versions(repository, *versions: str) -> list[str]:
    """Commit one `[tool.bumpversion]` version per commit, returning the SHAs."""
    shas = []
    for version in versions:
        _seed_commit(
            repository,
            "pyproject.toml",
            f'[tool.bumpversion]\ncurrent_version = "{version}"\n',
            f"Release {version}",
        )
        shas.append(head_sha())
    return shas


def test_commit_matrix_reads_each_version_from_its_commit(repository):
    """Every commit carries its own version, and the checkout never moves.

    Runs outside CI on purpose: reading from the object store leaves nothing
    to restore, so nothing needs guarding.
    """
    shas = _commit_versions(repository, "1.0.0", "1.1.0")
    pyproject = repository / "pyproject.toml"
    pyproject.write_text(
        '[tool.bumpversion]\ncurrent_version = "9.9.9"\n', encoding="UTF-8"
    )

    matrix = commit_matrix(shas)

    assert matrix is not None
    assert matrix["commit"] == tuple(shas)
    assert [entry.get("current_version") for entry in matrix.include] == [
        "1.0.0",
        "1.1.0",
    ]
    assert [entry["short_sha"] for entry in matrix.include] == [sha[:7] for sha in shas]
    assert head_sha() == shas[-1]
    assert "9.9.9" in pyproject.read_text(encoding="UTF-8")


def test_commit_matrix_of_nothing_is_none():
    assert commit_matrix(()) is None


def test_version_at_prefers_the_bumpversion_file(repository):
    """`.bumpversion.toml` wins over `pyproject.toml`, as bump-my-version reads it."""
    (sha,) = _commit_versions(repository, "1.0.0")
    _seed_commit(
        repository, ".bumpversion.toml", 'current_version = "2.0.0"\n', "Add config"
    )

    assert version_at(sha) == "1.0.0"
    assert version_at("HEAD") == "2.0.0"


def test_version_at_without_any_source(repository):
    """A commit carrying no bump-my-version configuration declares no version."""
    _seed_commit(repository, "pyproject.toml", '[project]\nname = "kiwi"\n', "Seed")
    assert version_at("HEAD") is None


def test_release_commit_is_the_newest_of_a_release_push(
    repository, tmp_path, monkeypatch
):
    """A job checks out the newest release commit of the push.

    `list_commits` reads the range oldest first, so the newest release commit
    is the last of the list. Reading its first entry would build and lint the
    older release.
    """
    _seed_commit(repository, "fruits.txt", "apple\n", "Seed")
    base = head_sha()
    shas = []
    for message in (
        "[changelog] Release v1.0.0",
        "[changelog] Post-release bump v1.0.0 → v1.0.1.dev0",
        "[changelog] Release v1.0.1",
        "[changelog] Post-release bump v1.0.1 → v1.0.2.dev0",
    ):
        _seed_commit(repository, "fruits.txt", f"{message}\n", message)
        shas.append(head_sha())
    _push_event(tmp_path, monkeypatch, base, shas[-1])
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")

    metadata = Metadata()
    assert metadata.release_commits is not None
    assert [commit.hash for commit in metadata.release_commits] == [shas[0], shas[2]]
    assert metadata.release_commit == shas[2]
    assert metadata.is_release_push is True


def test_release_commit_falls_back_to_the_event_commit(monkeypatch):
    """A push carrying no release commit names its own commit."""
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    metadata = Metadata()
    metadata.__dict__["release_commits"] = ()
    assert metadata.release_commit == "f" * 40


@pytest.mark.parametrize(
    ("ref", "release", "expected"),
    (
        pytest.param("refs/heads/main", True, True, id="release-on-main"),
        pytest.param("refs/heads/kiwi", True, False, id="release-on-a-branch"),
        pytest.param(None, True, False, id="release-outside-ci"),
        pytest.param("refs/heads/main", False, False, id="ordinary-push"),
    ),
)
def test_is_release_push(monkeypatch, ref, release, expected):
    """Only release commits landing on the release branch make a release push."""
    if ref:
        monkeypatch.setenv("GITHUB_REF", ref)
    else:
        monkeypatch.delenv("GITHUB_REF", raising=False)
    metadata = Metadata()
    metadata.__dict__["release_commits"] = (
        (Commit("a" * 40, "[changelog] Release v1.2.3"),) if release else ()
    )
    assert metadata.is_release_push is expected
