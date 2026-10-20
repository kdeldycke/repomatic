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

"""GitHub Actions environment accessors of {class}`~repomatic.metadata.core.Metadata`.

Every property here reads the workflow-run environment (the event payload,
the `GITHUB_*` variables) and nothing else: no git, no `pyproject.toml`.
"""

from __future__ import annotations

import json
import logging
import os
from functools import cached_property

from extra_platforms import is_github_ci

from ..components import is_awesome_repo
from ..git_ops import (
    get_repo_slug_from_remote,
)
from ..github.actions import (
    WorkflowEvent,
    get_github_event,
)
from ..github.gh import run_gh_command
from ..github.urls import repo_name, split_slug

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


class EnvironmentMetadata:
    """Workflow-run environment: the event payload and `GITHUB_*` variables.

    A concern mixin of {class}`~repomatic.metadata.core.Metadata`: never
    instantiated on its own.
    """

    @cached_property
    def github_event(self) -> dict[str, Any]:
        """Load the GitHub event payload from `GITHUB_EVENT_PATH`.

        GitHub Actions sets `GITHUB_EVENT_PATH` to a JSON file holding the
        complete webhook event payload.

        Delegates to {func}`repomatic.github.actions.get_github_event`, the one
        loader of the payload, which reads a missing file as an empty payload.
        Only the warning about a CI run missing the variable lives here, since
        the shared loader serves callers outside CI too.
        """
        if is_github_ci() and not os.environ.get("GITHUB_EVENT_PATH"):
            logging.warning("GITHUB_EVENT_PATH not set in environment.")
        event = get_github_event()
        if event:
            logging.debug("--- GitHub event payload ---")
            logging.debug(json.dumps(event, indent=4))
        return event

    @cached_property
    def event_type(self) -> WorkflowEvent | None:
        """Returns the type of event that triggered the workflow run.

        Maps {attr}`event_name` (the `GITHUB_EVENT_NAME` variable, set by
        GitHub Actions on every run) onto its
        {class}`~repomatic.github.actions.WorkflowEvent` member, so `schedule`
        and `workflow_dispatch` runs resolve to their own event instead of
        falling in a `None` hole that nulls every commit matrix.

        :return: The event, or `None` outside CI, or when `GITHUB_EVENT_NAME` is
            absent or names an event this project does not know. Each case is
            logged as a warning.
        """
        if not is_github_ci():
            logging.warning(
                "Cannot guess event type because we're not in a CI environment."
            )
            return None
        if not self.event_name:
            logging.warning(
                "Cannot guess event type because GITHUB_EVENT_NAME is not set."
            )
            return None
        try:
            return WorkflowEvent(self.event_name)
        except ValueError:
            logging.warning(f"Unrecognized workflow event {self.event_name!r}.")
            return None

    @cached_property
    def event_actor(self) -> str | None:
        """Returns the GitHub login of the user that triggered the workflow run."""
        return os.environ.get("GITHUB_ACTOR") or None

    @cached_property
    def event_sender_type(self) -> str | None:
        """Returns the type of the user that triggered the workflow run."""
        sender_type = self.github_event.get("sender", {}).get("type")
        if not sender_type:
            return None
        assert isinstance(sender_type, str)
        return sender_type

    @cached_property
    def is_bot(self) -> bool:
        """Returns `True` if the workflow was triggered by a bot or automated process.

        This is useful to only run some jobs on human-triggered events. Or skip jobs
        triggered by bots to avoid infinite loops.

        The sender type covers every GitHub App, which is how Dependabot and
        Renovate author their pull requests. The Dependabot login is a second
        signal for downstream repositories: `sender.type` is absent from the
        event payload outside `push` and `pull_request` (and empty when the
        payload cannot be read at all), and the login is then the only thing
        left to match on.

        The test is deliberately not `sender.type != "User"`, which would also
        classify an `Organization` sender as a bot.
        """
        return self.event_sender_type == "Bot" or self.event_actor == "dependabot[bot]"

    @cached_property
    def head_branch(self) -> str | None:
        """Returns the head branch name for pull request events.

        For pull request events, this is the source branch name
        (e.g., `update-mailmap`). For push events, returns `None` since
        there's no head branch concept.

        The branch name is extracted from the `GITHUB_HEAD_REF` environment variable,
        which is [only set for pull request events](https://docs.github.com/en/actions/learn-github-actions/variables).
        """
        return os.environ.get("GITHUB_HEAD_REF") or None

    @cached_property
    def event_name(self) -> str | None:
        """Returns the name of the event that triggered the workflow.

        Reads `GITHUB_EVENT_NAME`. This is the raw event name (`"push"`,
        `"pull_request"`, `"workflow_run"`), which {attr}`event_type` resolves
        to a {class}`~repomatic.github.actions.WorkflowEvent` member.
        """
        return os.environ.get("GITHUB_EVENT_NAME") or None

    @cached_property
    def job_name(self) -> str | None:
        """Returns the ID of the current job in the workflow.

        Reads `GITHUB_JOB`.
        """
        return os.environ.get("GITHUB_JOB") or None

    @cached_property
    def ref_name(self) -> str | None:
        """Returns the short ref name of the branch or tag.

        Reads `GITHUB_REF_NAME`.
        """
        return os.environ.get("GITHUB_REF_NAME") or None

    @cached_property
    def repo_name(self) -> str | None:
        """Returns the repository name without owner prefix.

        Derived from {attr}`repo_slug`.
        """
        slug = self.repo_slug
        return repo_name(slug) if slug else None

    @cached_property
    def is_awesome(self) -> bool:
        """Whether this is an awesome-list repository.

        Detected by the `awesome-` prefix on the repository name.
        """
        name = self.repo_name
        return bool(name and is_awesome_repo(name))

    @cached_property
    def repo_owner(self) -> str | None:
        """Returns the repository owner.

        Reads `GITHUB_REPOSITORY_OWNER`, falling back to the owner
        component of {attr}`repo_slug`.
        """
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or None
        if not owner and self.repo_slug:
            owner = split_slug(self.repo_slug)[0] or None
        return owner

    @cached_property
    def repo_slug(self) -> str | None:
        """Returns the `owner/name` slug for the current repository.

        Resolution order: the `GITHUB_REPOSITORY` variable (CI), the URL of the
        `origin` remote (offline), then `gh repo view`. The remote comes before
        `gh` because reading it costs no network call, and no `gh` download on
        a machine that lacks one.
        """
        slug = os.environ.get("GITHUB_REPOSITORY") or None
        if not slug:
            slug = get_repo_slug_from_remote()
            if slug:
                logging.debug(f"Detected repository slug from git remote: {slug}")
        if not slug:
            try:
                slug = (
                    run_gh_command(
                        [
                            "repo",
                            "view",
                            "--json",
                            "nameWithOwner",
                            "--jq",
                            ".nameWithOwner",
                        ],
                    ).strip()
                    or None
                )
            except RuntimeError:
                logging.debug("Failed to detect repository slug via gh CLI.")
        return slug

    @cached_property
    def repo_url(self) -> str | None:
        """Returns the full URL to the repository.

        Derived from {attr}`server_url` and {attr}`repo_slug`.
        """
        slug = self.repo_slug
        if slug:
            return f"{self.server_url}/{slug}"
        return None

    @cached_property
    def run_attempt(self) -> str | None:
        """Returns the run attempt number.

        Reads `GITHUB_RUN_ATTEMPT`.
        """
        return os.environ.get("GITHUB_RUN_ATTEMPT") or None

    @cached_property
    def run_id(self) -> str | None:
        """Returns the unique ID of the current workflow run.

        Reads `GITHUB_RUN_ID`.
        """
        return os.environ.get("GITHUB_RUN_ID") or None

    @cached_property
    def run_number(self) -> str | None:
        """Returns the run number for the current workflow.

        Reads `GITHUB_RUN_NUMBER`.
        """
        return os.environ.get("GITHUB_RUN_NUMBER") or None

    @cached_property
    def server_url(self) -> str:
        """Returns the GitHub server URL.

        Reads `GITHUB_SERVER_URL`, defaulting to `https://github.com`.
        """
        return os.environ.get("GITHUB_SERVER_URL") or "https://github.com"

    @cached_property
    def sha(self) -> str | None:
        """Returns the commit SHA that triggered the workflow.

        Reads `GITHUB_SHA`.
        """
        return os.environ.get("GITHUB_SHA") or None

    @cached_property
    def triggering_actor(self) -> str | None:
        """Returns the login of the user that initiated the workflow run.

        Reads `GITHUB_TRIGGERING_ACTOR`. This differs from
        {attr}`event_actor` (`GITHUB_ACTOR`) when a workflow is re-run by a
        different user.
        """
        return os.environ.get("GITHUB_TRIGGERING_ACTOR") or None

    @cached_property
    def workflow_ref(self) -> str | None:
        """Returns the full workflow reference.

        Reads `GITHUB_WORKFLOW_REF`. The format is
        `owner/repo/.github/workflows/name.yaml@refs/heads/branch`.
        """
        return os.environ.get("GITHUB_WORKFLOW_REF") or None
