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

"""Git commit-range logic of {class}`~repomatic.metadata.core.Metadata`.

Resolves the commit range an event bundles, the release commits inside it,
what those commits changed, and the per-commit matrices built by rewinding
the checkout. This is the one concern that can touch the repository state,
always restoring it (see `_restored_worktree`).
"""

from __future__ import annotations

import logging
import subprocess
from contextlib import contextmanager, nullcontext
from functools import cached_property

from extra_platforms import is_github_ci

from ..git_ops import (
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    RELEASE_COMMIT_PATTERN,
    SHORT_SHA_LENGTH,
    Commit,
    checkout,
    commit_exists,
    count_commits,
    current_branch,
    diff_names,
    fetch_deepen,
    get_commit,
    head_sha,
    list_commits,
    stash,
    stash_count,
    stash_pop,
)
from ..github.actions import (
    NULL_SHA,
    WorkflowEvent,
)
from ..github.matrix import Matrix
from ..release.binary import (
    BINARY_AFFECTING_PATHS,
    SKIP_BINARY_BUILD_BRANCHES,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path
    from typing import Any


class GitMetadata:
    """Commit ranges, changed files, and the per-commit matrices.

    A concern mixin of {class}`~repomatic.metadata.core.Metadata`: never
    instantiated on its own, and reads sibling concerns through `self`.
    """

    if TYPE_CHECKING:
        # Sibling-concern surface read through `self`: each stub mirrors
        # the descriptor another mixin (or the assembled `Metadata`
        # class) defines, so every concern type-checks on its own.

        @cached_property
        def event_type(self) -> WorkflowEvent | None:
            """See {class}`~repomatic.metadata.env.EnvironmentMetadata`."""

        @staticmethod
        def get_current_version() -> str | None:
            """See {class}`~repomatic.metadata.project.ProjectMetadata`."""

        @cached_property
        def github_event(self) -> dict[str, Any]:
            """See {class}`~repomatic.metadata.core.Metadata`."""

        @cached_property
        def head_branch(self) -> str | None:
            """See {class}`~repomatic.metadata.env.EnvironmentMetadata`."""

        @cached_property
        def script_entries(self) -> list[tuple[str, str, str]]:
            """See {class}`~repomatic.metadata.project.ProjectMetadata`."""

        @cached_property
        def sha(self) -> str | None:
            """See {class}`~repomatic.metadata.env.EnvironmentMetadata`."""

        @cached_property
        def workflow_files(self) -> list[Path]:
            """See {class}`~repomatic.metadata.core.Metadata`."""

        @cached_property
        def yaml_files(self) -> list[Path]:
            """See {class}`~repomatic.metadata.core.Metadata`."""

        @cached_property
        def zsh_files(self) -> list[Path]:
            """See {class}`~repomatic.metadata.core.Metadata`."""

    def git_stash_count(self) -> int:
        """Returns the number of stashes."""
        count = stash_count()
        logging.debug(f"Number of stashes in repository: {count}")
        return count

    def git_deepen(
        self, commit_hash: str, max_attempts: int = 10, deepen_increment: int = 50
    ) -> bool:
        """Deepen a shallow clone until the provided `commit_hash` is found.

        Progressively fetches more commits from the current repository until the
        specified commit is found or max attempts is reached.

        Returns `True` if the commit was found, `False` otherwise.
        """
        # Cache the current depth to avoid repeated subprocess calls.
        current_depth: int | None = None

        for attempt in range(max_attempts):
            if commit_exists(commit_hash):
                if attempt > 0:
                    logging.info(
                        f"Found commit {commit_hash} after {attempt} deepen "
                        "operation(s)."
                    )
                return True

            logging.debug(f"Commit {commit_hash} not found.")

            # Only compute depth if not cached yet.
            if current_depth is None:
                current_depth = count_commits()

            if attempt == max_attempts - 1:
                # We've exhausted all attempts.
                logging.error(
                    f"Cannot find commit {commit_hash} in repository after "
                    f"{max_attempts} deepen attempts. "
                    f"Final depth is {current_depth} commits."
                )
                return False

            logging.info(f"Commit {commit_hash} not found at depth {current_depth}.")
            logging.info(
                f"Deepening by {deepen_increment} commits (attempt "
                f"{attempt + 1}/{max_attempts})..."
            )

            try:
                fetch_deepen(deepen_increment)
            except subprocess.CalledProcessError as ex:
                logging.error(f"Failed to deepen repository: {ex}")
                return False
            # Update cached depth after successful fetch.
            current_depth = count_commits()
            logging.debug(
                f"Repository deepened successfully. New depth: {current_depth}"
            )

        return False

    @contextmanager
    def _restored_worktree(self) -> Iterator[None]:
        """Save the repository state, and restore it however the body exits.

        Stashes any local changes and records the initial ref (the canonical
        active branch name, like `main`, or the HEAD SHA when detached), then
        checks out and unstashes on the way out, exception included: a
        `checkout()` raising mid-scan must not leave the repository on a past
        commit with the user's changes still stashed.

        :raises RuntimeError: Outside a CI environment, where rewinding a
            developer's checkout is never worth the metadata.
        """
        if not is_github_ci():
            raise RuntimeError(
                "Local repository manipulations only allowed in CI environment"
            )
        init_ref = current_branch() or head_sha()
        logging.debug(f"Initial commit reference: {init_ref}")

        counter_before = self.git_stash_count()
        logging.debug("Try to stash local changes before our series of checkouts.")
        stash()
        counter_after = self.git_stash_count()
        logging.debug(
            "Stash counter changes after 'git stash' command: "
            f"{counter_before} -> {counter_after}"
        )
        assert counter_after >= counter_before
        need_unstash = counter_after > counter_before
        logging.debug(f"Need to unstash after checkouts: {need_unstash}")
        try:
            yield
        finally:
            logging.debug(f"Restore repository to {init_ref}.")
            checkout(init_ref)
            if need_unstash:
                logging.debug("Unstash local changes that were previously saved.")
                stash_pop()

    def commit_matrix(self, commits: Sequence[Commit] | None) -> Matrix | None:
        """Pre-compute a matrix of commits.

        ```{danger}
        This method temporarily modifies the state of the repository to
        compute version metadata from the past.

        To prevent any loss of uncommitted data, it stashes local changes
        before its checkouts and restores the initial state however the scan
        exits, through {meth}`_restored_worktree`.
        ```

        The list of commits is augmented with long and short SHA values, as well as
        current version. Most recent commit is first, oldest is last.

        Returns a ready-to-use matrix structure:

        ```{code-block} python
        {
            "commit": [
                "346ce664f055fbd042a25ee0b7e96702e95",
                "6f27db47612aaee06fdf08744b09a9f5f6c2",
            ],
            "include": [
                {
                    "commit": "346ce664f055fbd042a25ee0b7e96702e95",
                    "short_sha": "346ce66",
                    "current_version": "2.0.1",
                },
                {
                    "commit": "6f27db47612aaee06fdf08744b09a9f5f6c2",
                    "short_sha": "6f27db4",
                    "current_version": "2.0.0",
                },
            ],
        }
        ```
        """
        if not commits:
            return None

        current_commit = head_sha()

        # Whether we must go back in time in the git log and browse past
        # commits: always with several commits, and with a single one only
        # when HEAD is not already sitting on it.
        past_commit_lookup = len(commits) > 1 or current_commit != commits[0].hash
        restore: AbstractContextManager[None]
        if past_commit_lookup:
            logging.debug(
                "We need to look into the commit history. Inspect the initial state "
                "of the repository."
            )
            restore = self._restored_worktree()
        else:
            logging.debug(
                "No need to look into the commit history: repository is already "
                f"checked out at {current_commit}"
            )
            restore = nullcontext()

        matrix = Matrix()
        with restore:
            for commit in commits:
                if past_commit_lookup:
                    logging.debug(f"Checkout to commit {commit.hash}")
                    checkout(commit.hash)

                commit_metadata = {
                    "commit": commit.hash,
                    "short_sha": commit.hash[:SHORT_SHA_LENGTH],
                }

                logging.debug(f"Extract project version at commit {commit.hash}")
                current_version = self.get_current_version()
                if current_version:
                    commit_metadata["current_version"] = current_version

                matrix.add_variation("commit", [commit.hash])
                matrix.add_includes(commit_metadata)

        return matrix

    @cached_property
    def changed_files(self) -> tuple[str, ...] | None:
        """Returns the list of files changed in the current event's commit range.

        Uses `git diff --name-only` between the start and end of the commit range.
        Returns `None` if no commit range is available (e.g., outside CI).
        """
        if not self.commit_range:
            return None
        start, end = self.commit_range
        if not start or not end:
            return None
        try:
            return diff_names(start, end)
        except subprocess.CalledProcessError as ex:
            detail = ex.stderr.strip() if ex.stderr else ex
            logging.warning(f"Failed to get changed files from git diff: {detail}")
            return None

    @cached_property
    def binary_affecting_paths(self) -> tuple[str, ...]:
        """Path prefixes that affect compiled binaries for this project.

        Combines the static {data}`BINARY_AFFECTING_PATHS` (common files like
        `pyproject.toml`, `uv.lock`, `tests/`) with project-specific source
        directories derived from `[project.scripts]` in `pyproject.toml`.

        For example, a project with `mpm = "meta_package_manager.__main__:main"`
        adds `meta_package_manager/` as an affecting path. This makes the check
        reusable across downstream repositories without hardcoding source directories.
        """
        # Derive top-level source package directories from script entry points.
        source_dirs: set[str] = set()
        for _cli_id, module_id, _callable_id in self.script_entries:
            # Extract top-level package: "meta_package_manager.__main__" →
            # "meta_package_manager/".
            top_package = module_id.split(".")[0]
            source_dirs.add(f"{top_package}/")
        return BINARY_AFFECTING_PATHS + tuple(sorted(source_dirs))

    @cached_property
    def head_commit_message(self) -> str:
        """Returns `github.event.head_commit.message` from the event payload.

        Set for `push` events. Empty string for events that do not carry a
        head commit (`pull_request`, `schedule`, `workflow_dispatch`).
        """
        head_commit = self.github_event.get("head_commit") or {}
        return head_commit.get("message") or ""

    @cached_property
    def yaml_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one YAML file.

        Lets per-job lint gates short-circuit on pushes / PRs that don't
        touch YAML. Falls back to "repo contains any YAML file" when the
        commit range is unavailable (`workflow_dispatch`), preserving the
        existing behavior of those manual runs.
        """
        if self.changed_files is None:
            return bool(self.yaml_files)
        return any(f.endswith((".yaml", ".yml")) for f in self.changed_files)

    @cached_property
    def zsh_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one Zsh file.

        Falls back to "repo contains any Zsh file" when the commit range
        is unavailable.
        """
        if self.changed_files is None:
            return bool(self.zsh_files)
        zsh_set = set(self.zsh_files)
        return any(f in zsh_set for f in self.changed_files)

    @cached_property
    def workflows_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one GitHub workflow file.

        Falls back to "repo contains any workflow file" when the commit
        range is unavailable.
        """
        if self.changed_files is None:
            return bool(self.workflow_files)
        wf_set = set(self.workflow_files)
        return any(f in wf_set for f in self.changed_files)

    @cached_property
    def skip_binary_build(self) -> bool:
        """Returns `True` if binary builds should be skipped for this event.

        Binary builds are expensive and time-consuming. This property identifies
        contexts where the changes cannot possibly affect compiled binaries,
        allowing workflows to skip Nuitka compilation jobs.

        Three mechanisms are checked:

        1. **Branch name** — PRs from known non-code branches (documentation,
           `.mailmap`, `.gitignore`, etc.) are skipped.
        2. **Version-bump commit** — Push events whose head commit is a
           user-initiated version bump (`Bump (major|minor) version to `)
           are skipped: the bump merge changes only version strings and
           `uv.lock`, so the new binary differs from the previous one only
           in the baked-in version string. The
           `[changelog] Post-release bump ` prefix is deliberately *not*
           checked here: the `prepare-release` merge bundles the release
           commit with the post-release-bump commit, and the release
           commit must still produce its binary.
        3. **Changed files** — Push events where all changed files fall outside
           {attr}`binary_affecting_paths` are skipped. This avoids ~2h of Nuitka
           builds for documentation-only commits to `main`.
        """
        if self.head_branch and self.head_branch in SKIP_BINARY_BUILD_BRANCHES:
            logging.info(
                f"Branch {self.head_branch!r} is in SKIP_BINARY_BUILD_BRANCHES. "
                "Binary build will be skipped."
            )
            return True

        if (
            self.event_type == WorkflowEvent.push
            and self.head_commit_message
            and any(
                self.head_commit_message.startswith(prefix)
                for prefix in MANUAL_VERSION_BUMP_COMMIT_PREFIXES
            )
        ):
            logging.info(
                "Head commit is a user-initiated version bump "
                f"({self.head_commit_message.splitlines()[0]!r}). "
                "Binary build will be skipped."
            )
            return True

        # For push events, check if changed files affect binaries.
        if self.event_type == WorkflowEvent.push and self.changed_files is not None:
            affecting = self.binary_affecting_paths
            if not self.changed_files:
                # No changed files means nothing to build.
                logging.info("No changed files detected. Binary build will be skipped.")
                return True
            if not any(
                f.startswith(prefix) for f in self.changed_files for prefix in affecting
            ):
                logging.info(
                    f"No changed files match binary-affecting paths {affecting!r}. "
                    "Binary build will be skipped."
                )
                return True

        return False

    @cached_property
    def commit_range(self) -> tuple[str | None, str] | None:
        """Range of commits bundled within the triggering event.

        A workflow run is triggered by a singular event, which might encapsulate one or
        more commits. This means the workflow will only run once on the last commit,
        even if multiple new commits were pushed.

        This is critical for releases where two commits are pushed together:

        1. `[changelog] Release vX.Y.Z` — the release commit
        2. `[changelog] Post-release bump vX.Y.Z → vX.Y.Z` — the post-release bump

        Without extracting the full commit range, the release commit would be missed
        since `github.event.head_commit` only exposes the post-release bump.

        This property also enables processing each commit individually when we want to
        keep a carefully constructed commit history. The typical example is a pull
        request that is merged upstream but we'd like to produce artifacts (builds,
        packages, etc.) for each individual commit.

        The default `GITHUB_SHA` environment variable is not enough as it only points
        to the last commit. We need to inspect the commit history to find all new ones.
        New commits need to be fetched differently in `push` and `pull_request`
        events.

        ```{seealso}
        - https://stackoverflow.com/a/67204539
        - https://stackoverflow.com/a/62953566
        - https://stackoverflow.com/a/61861763
        ```

        ```{seealso}
        Pull request events on GitHub are a bit complex, see: [The Many SHAs of a GitHub Pull Request](https://www.kenmuse.com/blog/the-many-shas-of-a-github-pull-request/).
        ```
        """
        if not self.github_event or not self.event_type:
            return None
        # Pull request event.
        if self.event_type in (
            WorkflowEvent.pull_request,
            WorkflowEvent.pull_request_target,
        ):
            pr_data = self.github_event.get("pull_request", {})
            start = pr_data.get("base", {}).get("sha")
            # We need to checkout the HEAD commit instead of the artificial merge
            # commit introduced by the pull request.
            end = pr_data.get("head", {}).get("sha")
        # Push event.
        else:
            start = self.github_event.get("before")
            end = self.sha
        logging.debug(f"Commit range: {start} -> {end}")
        if not start or not end:
            logging.warning(f"Incomplete commit range: {start} -> {end}")
        return start, end

    @cached_property
    def current_commit(self) -> Commit:
        """Returns the current `Commit` object.

        Raises if `HEAD` cannot be resolved (an empty repository), mirroring the
        previous behavior where traversing an empty history raised too.
        """
        return get_commit("HEAD")

    @cached_property
    def current_commit_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of the current commit."""
        return self.commit_matrix((self.current_commit,))

    @cached_property
    def new_commits(self) -> tuple[Commit, ...] | None:
        """Returns list of all `Commit` objects bundled within the triggering event.

        This extracts **all commits** from the push event, not just `head_commit`.
        For releases, this typically includes both the release commit and the
        post-release bump commit, allowing downstream jobs to process each one.

        Commits are returned in chronological order (oldest first, most recent last).
        """
        if not self.commit_range:
            return None
        start, end = self.commit_range

        # Treat the null SHA as no start commit. GitHub sends this value when a tag is
        # created, since there is no previous commit to compare against.
        if start == NULL_SHA:
            logging.info(
                f"Start commit is null SHA ({NULL_SHA}), treating as no start commit."
            )
            start = None

        # Every branch below shells out to git, so mirror `changed_files`: a git
        # failure (a compiled binary run over a checkout git rejects as "dubious
        # ownership", a shallow clone that cannot be deepened) degrades to "range
        # unknown" instead of crashing the whole `metadata` command.
        try:
            # Sanity check: make sure both ends of the range exist in the repository.
            # Even though `start..end` excludes `start` from the result, git still
            # needs `start` present locally to resolve the range and walk history.
            for commit_id in (start, end):
                if not commit_id:
                    continue

                if not self.git_deepen(commit_id):
                    logging.warning(
                        "Skipping metadata extraction of the range of new commits."
                    )
                    return None

            if not start:
                logging.warning("No start commit found. Only one commit in range.")
                assert end
                return (get_commit(end),)

            # The `start..end` range already excludes `start`, so every returned
            # commit is a new one, in chronological order (oldest first).
            return list_commits(start, end)
        except subprocess.CalledProcessError as ex:
            detail = ex.stderr.strip() if ex.stderr else ex
            logging.warning(f"git failed while resolving new commits: {detail}")
            return None

    @cached_property
    def new_commits_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of new commits."""
        return self.commit_matrix(self.new_commits)

    @cached_property
    def new_commits_hash(self) -> tuple[str, ...] | None:
        """List all hashes of new commits."""
        return self.new_commits_matrix["commit"] if self.new_commits_matrix else None

    @cached_property
    def release_commits(self) -> tuple[Commit, ...] | None:
        """Returns list of `Commit` objects to be tagged within the triggering event.

        This filters `new_commits` to find release commits that need special handling:
        tagging, PyPI publishing, and GitHub release creation.

        This is essential because when a release is pushed, `github.event.head_commit`
        only exposes the post-release bump commit, not the release commit. By extracting
        all commits from the event (via `new_commits`) and filtering for release
        commits here, we ensure the release workflow can properly identify and process
        the `[changelog] Release vX.Y.Z` commit.

        We cannot identify a release commit based on the presence of a `vX.Y.Z` tag
        alone. That's because the tag is not present in the `prepare-release` pull
        request produced by the `changelog.yaml` workflow. The tag is created later
        by the `release.yaml` workflow, when the pull request is merged to `main`.

        Our best option is to identify a release based on the full commit message,
        using the template from the `changelog.yaml` workflow.
        """
        if not self.new_commits:
            return None
        return tuple(
            commit
            for commit in self.new_commits
            if RELEASE_COMMIT_PATTERN.fullmatch(commit.msg)
        )

    @cached_property
    def release_commits_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of release commits."""
        return self.commit_matrix(self.release_commits)

    @cached_property
    def release_commits_hash(self) -> tuple[str, ...] | None:
        """List all hashes of release commits."""
        return (
            self.release_commits_matrix["commit"]
            if self.release_commits_matrix
            else None
        )
