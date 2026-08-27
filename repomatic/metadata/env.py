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

import logging
import os
from functools import cached_property

from extra_platforms import is_github_ci

from ..git_ops import (
    get_repo_slug_from_remote,
)
from ..github.actions import (
    WorkflowEvent,
)
from ..github.gh import run_gh_command
from ..registry import is_awesome_repo

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


class EnvironmentMetadata:
    """Workflow-run environment: the event payload and `GITHUB_*` variables.

    A concern mixin of {class}`~repomatic.metadata.core.Metadata`: never
    instantiated on its own, and reads sibling concerns through `self`.
    """

    if TYPE_CHECKING:
        # Sibling-concern surface read through `self`: each stub mirrors
        # the descriptor another mixin (or the assembled `Metadata`
        # class) defines, so every concern type-checks on its own.

        @cached_property
        def github_event(self) -> dict[str, Any]:
            """See {class}`~repomatic.metadata.core.Metadata`."""

    @cached_property
    def event_type(self) -> WorkflowEvent | None:
        """Returns the type of event that triggered the workflow run.

        Maps {attr}`event_name` (the `GITHUB_EVENT_NAME` variable, set by
        GitHub Actions on every run) onto its
        {class}`~repomatic.github.actions.WorkflowEvent` member, so `schedule`
        and `workflow_dispatch` runs resolve to their own event instead of
        falling in a `None` hole that nulls every commit matrix.

        ```{caution}
        When `GITHUB_EVENT_NAME` is absent or unrecognized, falls back on the
        historical heuristic: a non-empty `GITHUB_BASE_REF` means a pull
        request ([only set for pull request events](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#default-environment-variables)),
        a present-but-empty one means a push.
        ```
        """
        if not is_github_ci():
            logging.warning(
                "Cannot guess event type because we're not in a CI environment."
            )
            return None
        if self.event_name:
            try:
                return WorkflowEvent(self.event_name)
            except ValueError:
                logging.warning(
                    f"Unrecognized workflow event {self.event_name!r};"
                    " falling back on the GITHUB_BASE_REF heuristic."
                )
        if "GITHUB_BASE_REF" not in os.environ:
            logging.warning(
                "Cannot guess event type because no GITHUB_BASE_REF env var found."
            )
            return None

        if bool(os.environ.get("GITHUB_BASE_REF")):
            return WorkflowEvent.pull_request
        return WorkflowEvent.push

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
        Renovate author their pull requests today. The explicit login list is
        kept as a second signal for downstream repositories: `sender.type` is
        absent from the event payload outside `push` and `pull_request` (and
        empty when the payload cannot be read at all), and the login is then
        the only thing left to match on.

        The test is deliberately not `sender.type != "User"`, which would also
        classify an `Organization` sender as a bot.
        """
        return self.event_sender_type == "Bot" or self.event_actor in (
            "dependabot[bot]",
            "dependabot-preview[bot]",
        )

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

        Derived from {attr}`repo_slug` by splitting on `/`.
        """
        slug = self.repo_slug
        return slug.split("/")[-1] if slug else None

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
        if not owner:
            slug = self.repo_slug
            if slug and "/" in slug:
                owner = slug.split("/")[0]
        return owner

    @cached_property
    def repo_slug(self) -> str | None:
        """Returns the `owner/name` slug for the current repository.

        Resolution order: `GITHUB_REPOSITORY` env var (CI), `gh repo view`
        (authenticated local), git remote URL parsing (offline fallback).
        """
        slug = os.environ.get("GITHUB_REPOSITORY") or None
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
        if not slug:
            slug = get_repo_slug_from_remote()
            if slug:
                logging.debug(f"Detected repository slug from git remote: {slug}")
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
