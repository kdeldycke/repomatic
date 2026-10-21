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

"""Read the event that started a GitHub Actions workflow run.

The runner writes the event payload to the file `GITHUB_EVENT_PATH` names. The
readers here take the defaults a command needs from it: the issue or pull
request a run is about, its author and number, and the repository's default
branch.
"""

from __future__ import annotations

import json
import logging
import os
from enum import StrEnum
from functools import cache
from pathlib import Path

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


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


def extract_workflow_filename(workflow_ref: str | None) -> str:
    """Extract the workflow filename from `GITHUB_WORKFLOW_REF`.

    :param workflow_ref: The full workflow reference, like
        `owner/repo/.github/workflows/name.yaml@refs/heads/branch`.
    :return: The workflow filename (like `name.yaml`), or an empty string
        if the reference is empty or malformed.
    """
    if not workflow_ref:
        return ""
    # Strip the @ref suffix, then take the basename.
    path_part = workflow_ref.split("@")[0]
    return path_part.rsplit("/", 1)[-1] if "/" in path_part else path_part


@cache
def get_github_event() -> dict[str, Any]:
    """Load the GitHub event payload from `GITHUB_EVENT_PATH`.

    Every reader here takes a default from the payload, so a payload that
    cannot be read degrades to no defaults instead of failing the command.

    :return: The parsed event payload, or an empty dict when the variable is
        unset, or the file is missing, unreadable, or holds no JSON object.
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        payload = json.loads(Path(event_path).read_text(encoding="UTF-8"))
    except (OSError, json.JSONDecodeError) as error:
        logging.warning(f"Cannot read the event payload {event_path}: {error}")
        return {}
    if not isinstance(payload, dict):
        logging.warning(f"The event payload {event_path} holds no JSON object.")
        return {}
    return payload


def get_event_pull_request() -> dict[str, Any]:
    """Return the event payload's `pull_request` node, empty when absent.

    Truthiness, not key presence, is the test every reader below shares. A
    payload carrying `pull_request` as an empty object has no PR to act on, and
    it has to read that way to {func}`is_pull_request` as well as to the
    default lookups, or the two would disagree on which node they read.
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


def get_default_branch() -> str | None:
    """Get the repository's default branch from the GitHub event payload.

    Every event that names a repository carries its `default_branch`, so a
    checkout detached at a raw SHA can still name the branch to open a pull
    request against.
    """
    branch = (get_github_event().get("repository") or {}).get("default_branch")
    return str(branch) if branch else None


def is_pull_request() -> bool:
    """Check if the current event is a pull request."""
    return bool(get_event_pull_request())
