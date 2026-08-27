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

"""GitHub Actions output formatting, annotations, and workflow events.

This module provides utilities for working with GitHub Actions: multiline
output formatting, workflow annotations, event payload loading, and
GitHub-specific constants and enums shared across multiple modules.

```{note} Concurrency quirks addressed by the workflows

**SHA-based groups (`release.yaml`):** the block sits on the
push-triggered entry workflow, not the reusable `_release-engine.yaml` it
calls. GitHub decides run cancellation from the entry workflow's group, and
a block on the engine lane (reached via `needs: build`) joins its group only
after the build lane finishes, too late to cancel queued or building runs.
`cancel-in-progress` is evaluated on the *new* workflow, not the old one. If
a regular commit is pushed while a release workflow is running, the new
workflow would cancel it (same group). Solution: release commits (freeze and
unfreeze) get a unique group keyed by `github.sha`, so they can never be
cancelled.

**Event-scoped groups (`changelog.yaml`):** `changelog.yaml` has
both `push` and `workflow_run` triggers. Without `event_name` in
the concurrency group, a fast-completing `workflow_run` event would
cancel the `push` event's `prepare-release` job, then skip
`prepare-release` itself (guarded by `if: event_name != 'workflow_run'`),
so it would never run. Including `event_name` prevents cross-event
cancellation.

**`workflow_run` checkout ref:** Always use `github.sha` (latest
commit on the default branch), never `workflow_run.head_sha` (the
commit that *triggered* the upstream workflow). After a release cycle
adds commits (freeze + unfreeze), `head_sha` is stale and produces
a tree that conflicts with current `main`.
```
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from random import randint
from tempfile import mkstemp

from click_extra import echo, prep_path

from ..compat import StrEnum
from ..git_ops import RELEASE_COMMIT_PREFIX
from .gh import run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


NULL_SHA = "0" * 40
"""The null SHA used by Git to represent a non-existent commit.

GitHub sends this value as the `before` SHA when a tag is created, since there is no
previous commit to compare against.
"""


MAX_STEP_OUTPUT_BYTES = 32 * 4096 - 1024
"""Ceiling for a single `$GITHUB_OUTPUT` value, in UTF-8 bytes.

A step output only exists to be read by a later step, and the two ways of
reading one both land it in the consumer's environment: `env:` mapping a
`steps.*.outputs.*` expression, and action inputs, which the runner exports as
`INPUT_*`. Linux caps a single `argv`/`envp` string at `MAX_ARG_STRLEN`, 32
pages, so a value past that makes the runner's `execve()` of `/usr/bin/bash`
fail with `E2BIG` before the step's own command exists:

```{code-block} text
##[error]An error occurred trying to start process '/usr/bin/bash' with
working directory '/home/runner/work/orchard/orchard'. Argument list too long
```

The 1 KiB reserve covers the `NAME=` prefix the kernel counts as part of the
same string, well beyond the longest name in use.

This ceiling only binds a value the environment has to carry. A report that
grows without bound belongs in a file instead: see {func}`format_file_output`,
which hands the consumer a path and leaves the content untrimmed.

```{caution}
This is a transport limit, counted in bytes, and is deliberately looser than
{data}`repomatic.github.pr_body.GITHUB_BODY_MAX_CHARS`, which is a content
limit counted in UTF-16 code units. A value can clear this one and still be
trimmed later by {func}`repomatic.github.pr_body.build_pr_body`, which is the
layer that leaves the reader a truncation notice.
```
"""


class WorkflowEvent(StrEnum):
    """Workflow events that cause a workflow to run.

    [List of events](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows).
    """

    branch_protection_rule = "branch_protection_rule"
    check_run = "check_run"
    check_suite = "check_suite"
    create = "create"
    delete = "delete"
    deployment = "deployment"
    deployment_status = "deployment_status"
    discussion = "discussion"
    discussion_comment = "discussion_comment"
    fork = "fork"
    gollum = "gollum"
    issue_comment = "issue_comment"
    issues = "issues"
    label = "label"
    merge_group = "merge_group"
    milestone = "milestone"
    page_build = "page_build"
    project = "project"
    project_card = "project_card"
    project_column = "project_column"
    public = "public"
    pull_request = "pull_request"
    pull_request_comment = "pull_request_comment"
    pull_request_review = "pull_request_review"
    pull_request_review_comment = "pull_request_review_comment"
    pull_request_target = "pull_request_target"
    push = "push"
    registry_package = "registry_package"
    release = "release"
    repository_dispatch = "repository_dispatch"
    schedule = "schedule"
    status = "status"
    watch = "watch"
    workflow_call = "workflow_call"
    workflow_dispatch = "workflow_dispatch"
    workflow_run = "workflow_run"


class AnnotationLevel(Enum):
    """Annotation levels for GitHub Actions workflow commands.

    Mirrors the three levels GitHub supports, even where the codebase only
    emits a subset.
    """

    ERROR = "error"
    WARNING = "warning"
    NOTICE = "notice"


class ReportAction(Enum):
    """What a job did to one item, as the markdown report spells it.

    Each member's value is the emoji-decorated label the report table shows,
    so rendering reads the label straight off the action instead of consulting
    a parallel mapping a new member could silently miss.

    The members are the union of the vocabularies every report needs, and each
    report uses the subset that applies to it: the changelog-to-release-notes
    sync ({mod}`~repomatic.github.release_sync`) never unsubscribes, and the
    notification sweep ({mod}`~repomatic.github.unsubscribe`) has nothing to
    call in sync. What they share is the outcome pair every dry-runnable
    sweep reports, which is why the vocabulary is defined once here rather
    than re-spelled per report.
    """

    DRY_RUN = "\U0001f441\ufe0f Dry-run"
    FAILED = "\u26a0\ufe0f Failed"
    SKIPPED = "\u2705 In sync"
    UNSUBSCRIBED = "\U0001f515 Unsubscribed"
    UPDATED = "\U0001f504 Updated"


def extract_workflow_filename(workflow_ref: str | None) -> str:
    """Extract the workflow filename from `GITHUB_WORKFLOW_REF`.

    :param workflow_ref: The full workflow reference, e.g.
        `owner/repo/.github/workflows/name.yaml@refs/heads/branch`.
    :return: The workflow filename (e.g. `name.yaml`), or an empty string
        if the reference is empty or malformed.
    """
    if not workflow_ref:
        return ""
    # Strip the @ref suffix, then take the basename.
    path_part = workflow_ref.split("@")[0]
    return path_part.rsplit("/", 1)[-1] if "/" in path_part else path_part


def generate_delimiter() -> str:
    """Generate a unique delimiter for GitHub Actions multiline output.

    GitHub Actions requires a unique delimiter to encode multiline values in
    `$GITHUB_OUTPUT`. This function generates a random delimiter that is
    extremely unlikely to appear in the output content.

    The delimiter format is `GHA_DELIMITER_NNNNNNNNN` where N is a digit,
    producing a 9-digit random suffix.

    :return: A unique delimiter string.

    ```{seealso}
    https://github.com/orgs/community/discussions/26288#discussioncomment-3876281
    ```
    """
    return f"GHA_DELIMITER_{randint(10**8, (10**9) - 1)}"


def trim_to_budget(text: str, budget: int, measure: Callable[[str], int]) -> str:
    """Keep the leading whole lines of *text* that fit in *budget*.

    Cutting on line boundaries keeps the trimmed markdown rendering: a table
    missing rows still renders, one cut mid-row does not.

    The one trimming loop behind both of GitHub's size ceilings, which count
    in different units: *measure* prices a line in whatever unit the caller's
    budget is denominated in (UTF-8 bytes for a step output, UTF-16 code units
    for a PR or issue body).

    :param text: Content to trim.
    :param budget: Available room, in *measure*'s unit.
    :param measure: Returns the size of one line in that unit.
    :return: The kept lines, right-stripped; empty when nothing fits.
    """
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        used += measure(line) + 1  # The joining newline.
        if used > budget:
            break
        kept.append(line)
    return "\n".join(kept).rstrip()


def _utf8_len(text: str) -> int:
    """Length of *text* in UTF-8 bytes, the unit `$GITHUB_OUTPUT` is capped in."""
    return len(text.encode("UTF-8"))


def trim_to_byte_budget(text: str, budget: int) -> str:
    """Keep the leading whole lines of *text* that fit in *budget* UTF-8 bytes.

    {func}`trim_to_budget` in the step-output unit. Trimming whole lines also
    keeps the result valid UTF-8, which slicing a byte string cannot promise.

    :param text: Content to trim.
    :param budget: Available room, in UTF-8 bytes.
    :return: The kept lines, right-stripped; empty when nothing fits.
    """
    return trim_to_budget(text, budget, _utf8_len)


def format_multiline_output(name: str, value: str) -> str:
    """Format a multiline value for GitHub Actions output.

    Produces output in the heredoc format required by `$GITHUB_OUTPUT`:

    ```{code-block} text

    name<<GHA_DELIMITER_NNNNNNNNN
    value line 1
    value line 2
    GHA_DELIMITER_NNNNNNNNN
    ```

    Values over {data}`MAX_STEP_OUTPUT_BYTES` are trimmed to fit, so a step
    reading this output cannot be killed by `E2BIG` before it starts.

    :param name: The output variable name.
    :param value: The multiline value.
    :return: Formatted string for `$GITHUB_OUTPUT`.
    """
    size = _utf8_len(value)
    if size > MAX_STEP_OUTPUT_BYTES:
        logging.warning(
            f"Step output {name!r} is {size} bytes, over the "
            f"{MAX_STEP_OUTPUT_BYTES}-byte limit a consuming step can receive: "
            "trimming to fit."
        )
        value = trim_to_byte_budget(value, MAX_STEP_OUTPUT_BYTES)
    delimiter = generate_delimiter()
    return f"{name}<<{delimiter}\n{value}\n{delimiter}"


def write_output_file(name: str, value: str) -> Path:
    """Write a step output's value to a file, and return that file's path.

    The file lands in `RUNNER_TEMP` where the runner exports it, which is
    per-job and cleaned up by the runner itself, and in the system temporary
    directory otherwise. Either is shared by every step of a job, which is what
    makes the handoff work: producer and consumer are separate processes on one
    filesystem.

    :param name: The step output name, which the filename carries so a spilled
        report is recognisable in a temporary directory.
    :param value: The content to write.
    :return: Path of the file holding *value*.
    """
    handle, path = mkstemp(
        prefix=f"repomatic-{name}-",
        suffix=".md",
        dir=os.getenv("RUNNER_TEMP") or None,
    )
    os.close(handle)
    spilled = Path(path)
    spilled.write_text(value, encoding="UTF-8")
    return spilled


def format_file_output(name: str, value: str) -> str:
    """Format a value as a file-backed output, for a consumer to read back.

    Writes *value* out with {func}`write_output_file` and names the step output
    `<name>_file`, so the consuming step receives a path instead of content:

    ```{code-block} text
    harvest_file=/home/runner/work/_temp/repomatic-harvest-8m1t0p.md
    ```

    This is the escape hatch from {data}`MAX_STEP_OUTPUT_BYTES`. A report grows
    with the repository it describes and has no ceiling of its own: the release
    notes a dependency sweep collects reached 269 KiB on one downstream repo,
    twice what the environment can carry, and trimming to fit is a loss of
    content, not a fix. Handing over a path keeps the environment holding a
    hundred-odd bytes whatever the report weighs, and leaves any trimming to
    {func}`repomatic.github.pr_body.build_pr_body`, which is the layer that
    knows GitHub's own body limit and marks the cut for the reader.

    :param name: The output variable name, before the `_file` suffix.
    :param value: The content to hand over.
    :return: Formatted string for `$GITHUB_OUTPUT`.
    """
    return f"{name}_file={write_output_file(name, value)}"


def emit_report(
    body: str,
    output: Path | None,
    output_format: str,
    key: str = "diff_table",
) -> None:
    """Write a markdown report to `--output`, optionally as a step output.

    The shared tail of every report-producing command: nothing is written
    when no output path is set or the body is empty; with
    `--output-format github-actions` the body is spilled to a file and the step
    output named `<key>_file` carries its path, for `$GITHUB_OUTPUT`
    consumption.

    A report is the one value here with no ceiling of its own, so it is the one
    that must not travel inline: see {func}`format_file_output`.

    :param body: The markdown report.
    :param output: The `--output` path (`None` to skip, `-` for stdout).
    :param output_format: `markdown` or `github-actions`.
    :param key: The step output variable name, before the `_file` suffix, for
        the `github-actions` format.
    """
    if output is None or not body:
        return
    if output_format == "github-actions":
        content = format_file_output(key, body)
    else:
        content = body
    echo(content, file=prep_path(output))


def read_file_output(name: str) -> str:
    """Read a value passed either as a file path or inline.

    The consuming half of {func}`format_file_output`: `<NAME>_FILE` holds the
    path of a file whose content is the value, while `<NAME>` holds the value
    itself. The path wins where both are set, the inline variable remaining for
    a caller that has not moved over, and for a workflow pinned to a release
    older than the CLI it invokes.

    :param name: The environment variable name, before the `_FILE` suffix.
    :return: The value, empty when neither variable is set.
    """
    path = os.getenv(f"{name}_FILE")
    if path:
        return Path(path).read_text(encoding="UTF-8")
    return os.getenv(name, "")


def emit_annotation(level: AnnotationLevel, message: str) -> None:
    """Emit a GitHub Actions workflow annotation.

    Prints a workflow command that creates an annotation visible in the GitHub
    Actions UI and PR checks.

    :param level: The annotation level.
    :param message: The annotation message.

    ```{seealso}
    https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#setting-an-error-message
    ```
    """
    print(f"::{level.value}::{message}")


@lru_cache(maxsize=1)
def get_github_event() -> dict[str, Any]:
    """Load the GitHub event payload from `GITHUB_EVENT_PATH`.

    :return: The parsed event payload, or empty dict if not available.
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    event_file = Path(event_path)
    if not event_file.exists():
        logging.warning(f"Event file not found: {event_path}")
        return {}
    return json.loads(  # type: ignore[no-any-return]
        event_file.read_text(encoding="UTF-8")
    )


def get_event_pull_request() -> dict[str, Any]:
    """Return the event payload's `pull_request` node, empty when absent.

    Truthiness, not key presence, is the test every reader below shares. A
    payload carrying `pull_request` as an empty object has no PR to act on,
    and it has to read that way to {func}`is_pull_request` as well as to the
    default lookups: testing `"pull_request" in event` here (as this code
    once did) let the two disagree, so `is_pull_request` reported a pull
    request while {func}`get_default_number` fell through to the issue branch.
    """
    return get_github_event().get("pull_request") or {}


def get_event_subject() -> dict[str, Any]:
    """Return the issue or pull request the current event is about.

    Pull requests win: the two nodes are mutually exclusive on the events
    these readers handle, and preferring the PR keeps the lookups reading the
    same node {func}`is_pull_request` reports on.

    :return: The subject node, or an empty dict when the event carries neither.
    """
    return get_event_pull_request() or get_github_event().get("issue") or {}


def get_default_author() -> str | None:
    """Get the issue/PR author from the GitHub event payload."""
    login = get_event_subject().get("user", {}).get("login")
    return str(login) if login else None


def get_default_number() -> int | None:
    """Get the issue/PR number from the GitHub event payload."""
    number = get_event_subject().get("number")
    return int(number) if number else None


def is_pull_request() -> bool:
    """Check if the current event is a pull request."""
    return bool(get_event_pull_request())


def cancel_superseded_runs(branch: str, current_run_id: str) -> int:
    """Cancel the in-progress and queued workflow runs of *branch*.

    Backs the `cancel-runs` command, fired when a pull request closes:
    GitHub's `concurrency` mechanism only cancels a run when a *new* run
    enters the same group, and closing a PR fires no such run, so the
    branch's live runs would otherwise burn CI minutes to completion.

    Every listed run is cancelled except two: *current_run_id* (the
    cancelling run itself), and any run whose head commit carries
    {data}`~repomatic.git_ops.RELEASE_COMMIT_PREFIX`. A run that fails to
    cancel (already finished, insufficient token scope) is logged and
    skipped so one straggler never aborts the sweep. The repository is
    resolved by the `gh` CLI from `GH_REPO` or the checkout, matching every
    other `gh api` call.

    ```{caution}
    The release guard is what makes this safe to point at a default branch.
    Every workflow's `cancel-in-progress` gate already spares a release run
    from *automatic* supersession, but a sweep like this one enters no
    concurrency group, so nothing else would stop it from killing the
    matrix that publishes a release. Cancelling a release run mid-flight
    costs the version its binaries permanently, since publishing locks the
    asset list (`claude.md` § A published release freezes what is missing
    from it).
    ```

    :param branch: Head branch whose runs to cancel.
    :param current_run_id: Run ID to spare (the caller's own run).
    :return: Number of runs cancelled.
    """
    cancelled = 0
    for status in ("in_progress", "queued"):
        listing = run_gh_command([
            "api",
            "--paginate",
            f"repos/{{owner}}/{{repo}}/actions/runs?branch={branch}&status={status}",
            "--jq",
            # Tab-separated so a display title carrying spaces stays one field.
            '.workflow_runs[] | "\\(.id)\\t\\(.display_title)"',
        ])
        for line in listing.splitlines():
            if not line.strip():
                continue
            run_id, _, display_title = line.partition("\t")
            if run_id == current_run_id:
                continue
            if display_title.startswith(RELEASE_COMMIT_PREFIX):
                logging.info(
                    f"Sparing release run {run_id}: {display_title!r} carries "
                    f"{RELEASE_COMMIT_PREFIX!r}."
                )
                continue
            logging.info(f"Cancelling run {run_id} (status: {status}).")
            try:
                run_gh_command([
                    "api",
                    "--method",
                    "POST",
                    f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/cancel",
                ])
            except RuntimeError as exc:
                logging.warning(f"Failed to cancel run {run_id}: {exc}")
                continue
            cancelled += 1
    return cancelled
