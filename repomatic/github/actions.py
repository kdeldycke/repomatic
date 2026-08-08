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

from ..compat import StrEnum
from .gh import run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


NULL_SHA = "0" * 40
"""The null SHA used by Git to represent a non-existent commit.

GitHub sends this value as the `before` SHA when a tag is created, since there is no
previous commit to compare against.
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


def format_multiline_output(name: str, value: str) -> str:
    """Format a multiline value for GitHub Actions output.

    Produces output in the heredoc format required by `$GITHUB_OUTPUT`:

    ```{code-block} text

    name<<GHA_DELIMITER_NNNNNNNNN
    value line 1
    value line 2
    GHA_DELIMITER_NNNNNNNNN
    ```

    :param name: The output variable name.
    :param value: The multiline value.
    :return: Formatted string for `$GITHUB_OUTPUT`.
    """
    delimiter = generate_delimiter()
    return f"{name}<<{delimiter}\n{value}\n{delimiter}"


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


def cancel_superseded_runs(branch: str, current_run_id: str) -> int:
    """Cancel the in-progress and queued workflow runs of *branch*.

    Backs the `cancel-runs` command, fired when a pull request closes:
    GitHub's `concurrency` mechanism only cancels a run when a *new* run
    enters the same group, and closing a PR fires no such run, so the
    branch's live runs would otherwise burn CI minutes to completion.

    Every listed run except *current_run_id* (the cancelling run itself) is
    cancelled. A run that fails to cancel (already finished, insufficient
    token scope) is logged and skipped so one straggler never aborts the
    sweep. The repository is resolved by the `gh` CLI from `GH_REPO` or the
    checkout, matching every other `gh api` call.

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
            ".workflow_runs[].id",
        ])
        for run_id in listing.split():
            if run_id == current_run_id:
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
