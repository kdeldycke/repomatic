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

from io import BytesIO
from pathlib import Path

import pytest
from click_extra import ClickException

from repomatic.github.token import PatPermissionResults
from repomatic.metadata import Metadata
from repomatic.tool_runner import ensure_binary, run_tool

TYPE_CHECKING = False
if TYPE_CHECKING:
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

TESTS_DIR = PROJECT_ROOT / "tests"
"""Root of the test suite."""

TEST_FILES = sorted(TESTS_DIR.rglob("*.py"))
"""Every Python module of the test suite, sorted for the same reason."""

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


@pytest.fixture(autouse=True)
def _reset_metadata():
    """Ensure each test gets a fresh Metadata singleton.

    Resets before and after every test so that ``@cached_property`` values
    computed with one test's monkeypatched env vars never leak into another.
    """
    Metadata.reset()
    yield
    Metadata.reset()


@pytest.fixture(autouse=True)
def _reset_binary_memo():
    """Drop `ensure_binary`'s per-process memo so tests stay independent.

    The memo is keyed on tool name only, so without the reset one test's
    installed path (often under its private `tmp_path`) would be replayed to
    every later test asking for the same tool.
    """
    yield
    ensure_binary.cache_clear()


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
