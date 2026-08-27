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

"""Shared pytest fixtures and cross-file test helpers."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import pytest
import yaml
from click_extra import ClickException

from repomatic import metrics, pypi
from repomatic.bundle import get_data_content
from repomatic.github.actions import get_github_event
from repomatic.github.token import PatPermissionResults
from repomatic.github.workflow_sync import (
    canonical_caller_permissions,
    extract_trigger_info,
)
from repomatic.lint_repo import _fetch_rulesets
from repomatic.metadata import Metadata
from repomatic.tool_runner import ensure_binary, run_tool

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

    from typing_extensions import Self

PROJECT_ROOT = Path(__file__).parent.parent
"""Root of the repository."""

PACKAGE_DIR = PROJECT_ROOT / "repomatic"
"""Root of the package whose sources the conformance tests scan."""

PACKAGE_FILES = sorted(PACKAGE_DIR.rglob("*.py"))
"""Every Python module shipped in the package.

Sorted because `@pytest.mark.parametrize` derives test IDs from iteration
order, and `pytest-xdist` aborts a run whose workers disagree on them.
"""

WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
"""Directory holding this repository's own workflow files."""


def _declares_concurrency(path: Path) -> bool:
    """Whether a workflow file carries a top-level `concurrency:` block."""
    return any(
        line.startswith("concurrency:")
        for line in path.read_text(encoding="UTF-8").splitlines()
    )


WORKFLOWS_WITH_CONCURRENCY_BLOCK = tuple(
    sorted(p.name for p in WORKFLOWS_DIR.glob("*.yaml") if _declares_concurrency(p))
)
"""Workflows that actually define a concurrency block, read off disk.

Derived rather than hand-listed because two test modules assert against it from
opposite directions, and a hand-listed pair drifted apart once already: a
workflow can be *exempt* from the requirement and still carry a block, which no
list of intentions can express.
"""

WORKFLOWS_WITHOUT_CONCURRENCY_BLOCK = tuple(
    sorted(p.name for p in WORKFLOWS_DIR.glob("*.yaml") if not _declares_concurrency(p))
)
"""The complement of {data}`WORKFLOWS_WITH_CONCURRENCY_BLOCK`."""


def skip_unless_tool_runs(name: str) -> None:
    """Skip the calling test when a registry tool cannot run on this machine.

    A test that lints generated output reads the tool's verdict off its exit
    code, and a `uvx` resolution that fails offline exits non-zero too. Probing
    with `--version` first keeps the two apart, so whatever the real invocation
    returns afterwards is the tool's opinion of the content rather than a fetch
    failure misread as a defect in the generator.

    Only a developer with no network ever takes the skip: CI is the enforcement
    point, and a tool it cannot fetch fails the job long before this runs.

    :param name: Registry key of the tool the caller is about to invoke.
    """
    try:
        exit_code = run_tool(name, extra_args=("--version",))
    except (ClickException, OSError) as exc:
        pytest.skip(f"{name} is unavailable: {exc}")
    if exit_code != 0:
        pytest.skip(f"{name} cannot run here: `--version` exited {exit_code}")


def load_workflow(workflow_name: str) -> dict[str, Any]:
    """Load and parse one of this repository's workflow YAML files.

    The one loader behind every test that reads a workflow off disk, so they
    all parse the same bytes the same way.
    """
    workflow_path = WORKFLOWS_DIR / workflow_name
    with workflow_path.open(encoding="UTF-8") as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


@pytest.fixture
def hermetic_git(monkeypatch):
    """Shield temporary git repos from the developer's own git configuration.

    Commit signing, hooks, templates and identity from the global or system
    config would otherwise leak into repos a test builds under `tmp_path` and
    break hermeticity in machine-specific ways.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _reset_process_caches() -> None:
    """Clear every process-level memo a test could have poisoned.

    The production caches assume their inputs are immutable for the life of
    the process, which is true of a CLI run and false of a test session: a
    monkeypatched `get_data_content` or a rewritten fixture project would
    otherwise leak one test's world into the next through them.
    """
    Metadata.reset()
    get_github_event.cache_clear()
    pypi._FETCH_MEMO.clear()
    _fetch_rulesets.cache_clear()
    get_data_content.cache_clear()
    extract_trigger_info.cache_clear()
    canonical_caller_permissions.cache_clear()
    metrics._LAST_FETCH_REASONS.clear()


@pytest.fixture(autouse=True)
def _reset_metadata():
    """Ensure each test gets fresh singleton, event and content caches.

    Resets before and after every test so that ``@cached_property`` values
    computed with one test's monkeypatched env vars never leak into another,
    and so the process-level memos never serve another test's stubs.
    """
    _reset_process_caches()
    yield
    _reset_process_caches()


@pytest.fixture(autouse=True)
def _no_real_gh_subprocess():
    """Refuse to shell out to a real `gh` from the suite.

    A test that patches no `gh` seam at all used to run the actual binary
    against whatever credentials the machine carries, hitting the network once
    per unmocked check: `tests/test_lint_repo.py` spent seconds per test that
    way, and its results depended on the developer's `gh auth` state. Failing
    the call instead makes every such check report "could not run", which is
    the outcome an offline CI runner would see anyway.

    Tests wanting specific `gh` behavior patch a higher layer
    (`run_gh_command`, `gh_api_json`) or re-patch this one; either shadows the
    stub for their duration.
    """
    refused = CompletedProcess(
        args=["gh"],
        returncode=1,
        stdout="",
        stderr="gh is not invoked for real in the test suite.",
    )
    with patch("repomatic.github.gh.run", return_value=refused):
        yield


@pytest.fixture(autouse=True)
def _stub_gh_executable():
    """Keep the suite off the network when resolving the `gh` binary.

    `run_gh_command` resolves a registry-pinned `gh` through
    {func}`~repomatic.tool_runner.ensure_binary`, which downloads and
    checksum-verifies an archive. Every test that exercises a `gh` path
    already patches the subprocess call, so the resolution is pure overhead
    and would make the suite depend on the network. Tests covering the
    resolver itself patch `ensure_binary` and call it directly.
    """
    with patch("repomatic.github.gh.gh_executable", return_value="gh"):
        yield


@pytest.fixture(autouse=True)
def _reset_binary_memo():
    """Drop `ensure_binary`'s per-process memo so tests stay independent.

    The memo is keyed on tool name only, so without the reset one test's
    installed path (often under its private `tmp_path`) would be replayed to
    every later test asking for the same tool.
    """
    yield
    ensure_binary.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_user_config(isolated_app_dir):
    """Hide the developer's real `repomatic` configuration from the suite.

    Any config file in the host application folder (`~/.config/repomatic` on
    Unix, `~/Library/Application Support/repomatic` on macOS) is discovered by
    every in-process `CliRunner().invoke(repomatic, ...)`, so assertions would
    depend on the machine they run on: a local setting can pass or fail a test
    CI cannot reproduce.

    Autouse alias of click-extra's `isolated_app_dir` fixture (registered by
    its `pytest11` entry point), which repoints `click.get_app_dir`-based
    config discovery at a fresh per-test directory. `HOME` stays intact and
    the override does not propagate to subprocesses; tests exercising config
    loading pass an explicit path, which bypasses the default search and is
    unaffected.
    """
    return isolated_app_dir


class FakeResponse:
    """Minimal `urlopen` response double: a byte body behind a context manager.

    Stands in for the object `repomatic.http.get_json` reads, so network tests
    patch `repomatic.http.urlopen` with `return_value=FakeResponse(...)`.
    """

    def __init__(self, data: bytes) -> None:
        self._data = BytesIO(data)

    def read(self) -> bytes:
        return self._data.read()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def pat_results(**overrides: tuple[bool, str]) -> PatPermissionResults:
    """Build a `PatPermissionResults` where every check passes by default.

    Each keyword names a `PatPermissionResults` field and replaces its
    `(passed, message)` tuple, so a test spells out only the checks whose
    outcome it cares about and the rest read as the happy path.

    :param overrides: Field name to its `(passed, message)` result.
    :return: The assembled results object.
    """
    return PatPermissionResults(**{
        "administration": (True, "Administration: token has access"),
        "contents": (True, "Contents: token has access"),
        "issues": (True, "Issues: token has access"),
        "pull_requests": (True, "Pull requests: token has access"),
        "vulnerability_alerts": (
            True,
            "Dependabot alerts: token has access, alerts enabled",
        ),
        "workflows": (True, "Workflows: token has access"),
        **overrides,
    })


def metadata_from_pyproject(tmp_path: Path, monkeypatch, content: str) -> Metadata:
    """Build a `Metadata` reading a throwaway `pyproject.toml` holding *content*.

    Repointing `Metadata.pyproject_path` is what isolates the instance from the
    repository's own `pyproject.toml`: the class reads that path lazily, so the
    patch has to be in place before the first property is touched. The autouse
    `_reset_metadata` fixture clears the cached values afterwards.

    :param tmp_path: Directory to write the file into.
    :param monkeypatch: The test's monkeypatch fixture.
    :param content: Full `pyproject.toml` source.
    :return: A `Metadata` bound to the written file.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content, encoding="UTF-8")
    monkeypatch.setattr(Metadata, "pyproject_path", pyproject)
    return Metadata()


@pytest.fixture
def cache_env(monkeypatch, tmp_path):
    """Point the repomatic cache at a per-test directory, auto-purge disabled.

    Returns the cache root path so tests can assert on its contents.
    """
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "0")
    return cache_dir


@contextmanager
def patch_gh(module: str, **mock_kwargs):
    """Patch a module's `gh` dispatch points with one shared mock.

    Read paths go through `gh_api_json` (which calls the `gh` module's
    `run_gh_command`) while write paths call the name imported into *module*
    directly; patching both with the same mock keeps a test's single call
    sequence intact across the two layers. Bind the module with
    `functools.partial` for a per-file shorthand.
    """
    mock_gh = Mock(**mock_kwargs)
    with (
        patch(f"{module}.run_gh_command", mock_gh),
        patch("repomatic.github.gh.run_gh_command", mock_gh),
    ):
        yield mock_gh


def attribute_docstrings(body: list[ast.stmt]):
    """Yield `(line, owner, text)` for each attribute docstring in *body*.

    An attribute docstring is the bare string literal following an assignment.
    `ast.get_docstring` cannot see one, yet it is this project's convention for
    documenting a dataclass field or a module constant, and
    `automodule ... :undoc-members:` renders it.
    """
    owner = ""
    for statement in body:
        if (
            owner
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            yield statement.lineno, owner, statement.value.value
        if isinstance(statement, ast.Assign):
            targets = [t for t in statement.targets if isinstance(t, ast.Name)]
            owner = targets[0].id if targets else ""
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            owner = statement.target.id
        else:
            owner = ""
