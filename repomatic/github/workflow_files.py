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

"""Find, read and walk the GitHub Actions workflow files of a repository.

Every reader of a workflow goes through this module: the repository lint, the
workflow lint, the CI status report, `repomatic init` and the pin updaters. So
they agree on which files count as workflows (both extensions GitHub accepts),
on how a file parses, and on what a job or a step is.

A leaf module: it imports nothing from the rest of the package, so any module
can use it without the risk of an import cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any, Final

WORKFLOW_DIR: Final[Path] = Path(".github/workflows")
"""Directory GitHub reads a repository's workflows from, relative to its root.

`repomatic init` writes the workflows there and every check reads them from
there, so the generator and the checks cannot disagree about where a workflow
lives.
"""

GITHUB_YAML_PATTERNS: Final[tuple[str, ...]] = (
    ".github/workflows/*.yaml",
    ".github/workflows/*.yml",
    ".github/actions/**/*.yaml",
    ".github/actions/**/*.yml",
)
"""Globs matching every workflow and composite-action file of a repository.

Rooted at the repository root rather than at `.github/`, so the same patterns
work against the current directory and against an arbitrary target tree. Both
`.yml` and `.yaml` are listed because GitHub accepts either, whatever this
project's own [long-extension convention](https://repomatic.net)
prefers: a downstream repository is free to have picked the short one.

The pin updaters read the pins to bump from these files, and `repomatic init`
reads them to floor a new pin. The two must agree on which files carry a pin,
or `init` would floor against a file `sync-workflow-pins` never bumps.
"""

WORKFLOW_SUFFIXES: Final[frozenset[str]] = frozenset((".yaml", ".yml"))
"""File extensions GitHub reads a workflow from."""

_YAML_LOADER: Final = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
"""The fastest safe YAML loader this PyYAML build provides.

`CSafeLoader` binds libyaml and parses about ten times faster than the
pure-Python `SafeLoader`. Both resolve the same YAML 1.1 types, so a document
reads the same through either. A PyYAML built without libyaml lacks the C
loader, which is why the pure-Python one stays as the fallback.
"""


def github_yaml_paths(root: Path) -> list[Path]:
    """Every workflow and composite-action file under *root*.

    :param root: Root of the repository to scan.
    :return: The files matching {data}`GITHUB_YAML_PATTERNS`, sorted.
    """
    return sorted({
        path for pattern in GITHUB_YAML_PATTERNS for path in root.glob(pattern)
    })


def workflow_paths(directory: Path = WORKFLOW_DIR) -> list[Path]:
    """Every workflow file in *directory*.

    :param directory: Directory holding the workflow files.
    :return: The `.yaml` and `.yml` files, sorted. Empty when the directory
        does not exist.
    """
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix in WORKFLOW_SUFFIXES and path.is_file()
    )


def read_workflow_texts(directory: Path = WORKFLOW_DIR) -> dict[Path, str]:
    """Read every workflow file in *directory*, skipping the unreadable ones.

    The raw-text half of the workflow walk, for the checks that match against
    the file as written rather than as parsed: an argument inside a folded
    scalar is one opaque string to a YAML parser, whichever way the file is
    loaded.

    :param directory: Directory holding the workflow files.
    :return: A mapping of path to file content, in sorted path order.
    """
    texts: dict[Path, str] = {}
    for path in workflow_paths(directory):
        try:
            texts[path] = path.read_text(encoding="UTF-8")
        except OSError as error:
            logging.warning(f"Could not read {path}: {error}")
    return texts


def load_workflow(text: str) -> dict[str, Any] | None:
    """Parse the text of one workflow file.

    :param text: The file content.
    :return: The parsed document, or `None` when *text* is not YAML or does not
        parse into a mapping, which is the only shape a workflow can take.
    """
    try:
        data = yaml.load(text, Loader=_YAML_LOADER)
    except yaml.YAMLError as error:
        logging.debug(f"Workflow text does not parse as YAML: {error}")
        return None
    return data if isinstance(data, dict) else None


def parse_workflow_texts(texts: Mapping[Path, str]) -> dict[Path, dict[str, Any]]:
    """Parse pre-read workflow texts, keeping the ones that hold a mapping.

    The parsed half of the workflow walk. A file that does not parse is
    reported as a warning naming it, then skipped.

    :param texts: Raw file contents keyed by path, from
        {func}`read_workflow_texts`.
    :return: A mapping of path to parsed document.
    """
    workflows: dict[Path, dict[str, Any]] = {}
    for path, text in texts.items():
        try:
            data = yaml.load(text, Loader=_YAML_LOADER)
        except yaml.YAMLError as error:
            logging.warning(f"Could not parse {path}: {error}")
            continue
        if isinstance(data, dict):
            workflows[path] = data
    return workflows


def load_workflows(directory: Path = WORKFLOW_DIR) -> dict[Path, dict[str, Any]]:
    """Read and parse every workflow file in *directory*.

    :param directory: Directory holding the workflow files.
    :return: A mapping of path to parsed document, in sorted path order.
    """
    return parse_workflow_texts(read_workflow_texts(directory))


def workflow_triggers(data: object) -> dict[str, Any]:
    """Extract a parsed workflow's `on:` mapping.

    ```{note}
    PyYAML follows YAML 1.1, where a bare `on` key parses as the boolean `True`
    while a quoted `"on"` stays a string. Both spellings occur in the wild, so
    every reader of a workflow's triggers has to try the boolean key first and
    the string key second. Resolving that here once keeps the quirk from being
    re-remembered at each call site.
    ```

    :param data: A parsed workflow, from {func}`load_workflow`.
    :return: The trigger mapping, empty when *data* is not a mapping or declares
        no triggers.
    """
    if not isinstance(data, dict):
        return {}
    triggers = data.get(True, data.get("on"))
    return triggers if isinstance(triggers, dict) else {}


def jobs_of(data: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield `(job_id, job)` for every job mapping of one workflow.

    The one place the "a job is a mapping" guard lives: a `jobs:` key that is
    not a mapping yields nothing, and a malformed job (a string, a list) is
    skipped, so no walker trips over either.

    :param data: One parsed workflow document.
    """
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job_id, job in jobs.items():
        if isinstance(job, dict):
            yield str(job_id), job


def steps_of(job: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every step mapping of one job, with the guard of {func}`jobs_of`.

    :param job: One job mapping.
    """
    steps = job.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if isinstance(step, dict):
            yield step


def iter_jobs(
    workflows: Mapping[Path, Mapping[str, Any]],
) -> Iterator[tuple[Path, str, dict[str, Any]]]:
    """Yield `(path, job_id, job)` for every job across *workflows*.

    :param workflows: Parsed workflow documents, keyed by path.
    """
    for path, data in workflows.items():
        for job_id, job in jobs_of(data):
            yield path, job_id, job


def iter_steps(
    workflows: Mapping[Path, Mapping[str, Any]],
) -> Iterator[tuple[Path, str, dict[str, Any]]]:
    """Yield `(path, job_id, step)` for every step across *workflows*.

    :param workflows: Parsed workflow documents, keyed by path.
    """
    for path, job_id, job in iter_jobs(workflows):
        for step in steps_of(job):
            yield path, job_id, step


def literal_runners(
    workflow_dir: Path = WORKFLOW_DIR,
    workflows: Mapping[Path, Mapping[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """Every runner image this repository names outright, and where.

    Only literals: a value built from an expression (`${{ matrix.os }}`) names
    no image here, and the axis it draws from is checked at its definition. A
    thin caller declares no `steps:` and runs on whatever the reusable workflow
    chose, which is that workflow's business rather than this repository's.

    Separate from {data}`~repomatic.matrix.axes.KNOWN_RUNNERS`, and
    deliberately so. That set is what this project has *chosen*; this function
    reports what it is *running*, and the two diverge exactly when something has
    been left behind. Callers wanting "an image this repository has a stake in"
    need the union of both.

    :param workflow_dir: Directory holding the workflow files. Ignored when
        *workflows* is supplied.
    :param workflows: Pre-parsed workflows, read from disk when `None`.
    :return: A mapping of runner label to the `file.yaml:job-id` locations
        naming it, empty when no job names one literally.
    """
    if workflows is None:
        workflows = load_workflows(workflow_dir)
    seen: dict[str, list[str]] = {}
    for path, job_id, job in iter_jobs(workflows):
        if "steps" not in job:
            continue
        runner = job.get("runs-on")
        if not isinstance(runner, str) or "${{" in runner:
            continue
        seen.setdefault(runner, []).append(f"{path.name}:{job_id}")
    return seen
