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
what those commits changed, and the per-commit matrices. Every read goes
through the git object store, so the checkout and the working tree never
move.
"""

from __future__ import annotations

import logging
from functools import cached_property
from pathlib import Path

import tomlrt

from ..git_ops import (
    NULL_SHA,
    SHORT_SHA_LENGTH,
    Commit,
    GitError,
    deepen_until_related,
    diff_names,
    fetch_commit,
    get_commit,
    head_sha,
    list_commits,
    show_file,
)
from ..github.event import WorkflowEvent
from ..matrix.model import Matrix
from ..pyproject import resolve_source_paths
from ..release.protocol import (
    BINARY_AFFECTING_PATHS,
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    RELEASE_COMMIT_PATTERN,
    SKIP_BINARY_BUILD_BRANCHES,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import Any, Final

    from ..config import Config
    from ..file_inventory import FileInventory


BUMPVERSION_SOURCES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (".bumpversion.toml", ()),
    ("pyproject.toml", ("tool", "bumpversion")),
)
"""Files bump-my-version reads `current_version` from, in lookup order.

Each file comes with the path of the table holding the key: the top level of
`.bumpversion.toml`, `[tool.bumpversion]` of `pyproject.toml`.
"""


def version_at(ref: str) -> str | None:
    """Return the version bump-my-version manages, as committed at *ref*.

    Reads {data}`BUMPVERSION_SOURCES` from the object store, so a past commit
    is read without a checkout. A file that is absent at *ref*, or does not
    parse, is skipped like a file without the key.

    :param ref: The commit to read.
    :return: The `current_version` value, or `None` when no source holds one.
    """
    for filename, table_path in BUMPVERSION_SOURCES:
        content = show_file(ref, filename)
        if content is None:
            continue
        try:
            table: Any = tomlrt.loads(content)
        except tomlrt.TOMLParseError:
            logging.debug(f"{filename} does not parse at {ref}.")
            continue
        for key in table_path:
            table = table.get(key)
            if not isinstance(table, dict):
                break
        else:
            version = table.get("current_version")
            if version is not None:
                return str(version)
    return None


def commit_matrix(shas: Sequence[str] | None) -> Matrix | None:
    """Pre-compute a matrix of commits, in the order given.

    Each entry carries the long and short SHA, and the version the commit
    declares, read by {func}`version_at`. Returns a ready-to-use matrix
    structure:

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

    :param shas: Full commit SHAs.
    :return: The matrix, or `None` when *shas* is empty.
    """
    if not shas:
        return None
    matrix = Matrix()
    for sha in shas:
        entry = {"commit": sha, "short_sha": sha[:SHORT_SHA_LENGTH]}
        version = version_at(sha)
        if version:
            entry["current_version"] = version
        matrix.add_variation("commit", [sha])
        matrix.add_includes(entry)
    return matrix


def _touches(changed: Iterable[str], files: Iterable[Path]) -> bool:
    """Whether one of the *changed* paths is one of *files*.

    `git diff` names files as repository-relative strings, while the file
    inventory holds `Path` objects: each name is converted before the lookup,
    since a string never equals a `Path`.
    """
    known = frozenset(files)
    return any(Path(name) in known for name in changed)


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
        def config(self) -> Config:
            """See {class}`~repomatic.metadata.project.ProjectMetadata`."""

        @cached_property
        def event_type(self) -> WorkflowEvent | None:
            """See {class}`~repomatic.metadata.env.EnvironmentMetadata`."""

        @cached_property
        def files(self) -> FileInventory:
            """See {class}`~repomatic.metadata.core.Metadata`."""

        @cached_property
        def github_event(self) -> dict[str, Any]:
            """See {class}`~repomatic.metadata.core.Metadata`."""

        @cached_property
        def head_branch(self) -> str | None:
            """See {class}`~repomatic.metadata.env.EnvironmentMetadata`."""

        @cached_property
        def pyproject_toml(self) -> dict[str, Any]:
            """See {class}`~repomatic.metadata.project.ProjectMetadata`."""

        @cached_property
        def script_entries(self) -> list[tuple[str, str, str]]:
            """See {class}`~repomatic.metadata.project.ProjectMetadata`."""

        @cached_property
        def sha(self) -> str | None:
            """See {class}`~repomatic.metadata.env.EnvironmentMetadata`."""

    @cached_property
    def changed_files(self) -> tuple[str, ...] | None:
        """Returns the files changed in the current event's commit range.

        Diffs the trees of the two ends of {attr}`commit_range`. An end that is
        missing from a shallow clone is fetched first, on its own: the diff
        needs that commit's tree, not the history in between.

        Returns `None` when the range is unknown: outside CI, on a push that
        creates a branch or a tag (its start is
        {data}`~repomatic.git_ops.NULL_SHA`), or when git cannot produce an
        end of the range.
        """
        if not self.commit_range:
            return None
        start, end = self.commit_range
        if not start or not end or start == NULL_SHA:
            return None
        try:
            if not (fetch_commit(start) and fetch_commit(end)):
                logging.warning(
                    f"Cannot fetch {start} or {end}: changed files are unknown."
                )
                return None
            return diff_names(start, end)
        except GitError as error:
            logging.warning(f"Failed to get changed files from git diff: {error}")
            return None

    @cached_property
    def binary_affecting_paths(self) -> tuple[str, ...]:
        """Path prefixes that affect compiled binaries for this project.

        Combines the static
        {data}`~repomatic.release.protocol.BINARY_AFFECTING_PATHS` (common
        files like `pyproject.toml`, `uv.lock`, `tests/`) with the project's
        source directories, from two places:

        - The top-level package of each `[project.scripts]` entry point. A
          project with `mpm = "meta_package_manager.__main__:main"` adds
          `meta_package_manager/`.
        - The `[tool.repomatic] workflow.source-paths` directories, or the
          import name they default to. This is what covers a `src/` layout,
          whose package does not sit at the root the entry point names.

        This makes the check reusable across downstream repositories without
        hardcoding source directories.
        """
        source_dirs = {
            f"{module_id.split('.')[0]}/"
            for _cli_id, module_id, _callable_id in self.script_entries
        }
        source_dirs.update(
            f"{path.strip('/')}/"
            for path in resolve_source_paths(self.config, self.pyproject_toml) or ()
        )
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
            return bool(self.files.yaml_files)
        return any(f.endswith((".yaml", ".yml")) for f in self.changed_files)

    @cached_property
    def zsh_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one Zsh file.

        Falls back to "repo contains any Zsh file" when the commit range
        is unavailable.
        """
        if self.changed_files is None:
            return bool(self.files.zsh_files)
        return _touches(self.changed_files, self.files.zsh_files)

    @cached_property
    def workflows_changed(self) -> bool:
        """Returns `True` when the current event's commit range touches at
        least one GitHub workflow file.

        Falls back to "repo contains any workflow file" when the commit
        range is unavailable.
        """
        if self.changed_files is None:
            return bool(self.files.workflow_files)
        return _touches(self.changed_files, self.files.workflow_files)

    @cached_property
    def skip_binary_build(self) -> bool:
        """Returns `True` if binary builds should be skipped for this event.

        Binary builds are expensive and time-consuming. This property identifies
        contexts where the changes cannot possibly affect compiled binaries,
        allowing workflows to skip Nuitka compilation jobs.

        Three mechanisms are checked:

        1. **Branch name**: PRs from known non-code branches (documentation,
           `.mailmap`, `.gitignore`, etc.) are skipped.
        2. **Version-bump commit**: Push events whose head commit is a
           user-initiated version bump (`Bump (major|minor) version to `)
           are skipped: the bump merge changes only version strings and
           `uv.lock`, so the new binary differs from the previous one only
           in the baked-in version string. The
           `[changelog] Post-release bump ` prefix is deliberately *not*
           checked here: the `prepare-release` merge bundles the release
           commit with the post-release-bump commit, and the release
           commit must still produce its binary.
        3. **Changed files**: Push events where all changed files fall outside
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

        1. `[changelog] Release vX.Y.Z`: the release commit
        2. `[changelog] Post-release bump vX.Y.Z → vX.Y.Z`: the post-release bump

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
    def current_commit_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of the current commit."""
        return commit_matrix((head_sha(),))

    @cached_property
    def new_commits(self) -> tuple[Commit, ...] | None:
        """Returns list of all `Commit` objects bundled within the triggering event.

        This extracts **all commits** from the push event, not just `head_commit`.
        For releases, this typically includes both the release commit and the
        post-release bump commit, allowing downstream jobs to process each one.

        Commits are returned in chronological order (oldest first, most recent last).

        A shallow clone is completed as far as the range needs: both ends are
        fetched, then the clone is deepened until they share history. See
        {func}`~repomatic.git_ops.deepen_until_related` for why both ends
        being present is not enough.
        """
        if not self.commit_range:
            return None
        start, end = self.commit_range

        # Treat the null SHA as no start commit. GitHub sends this value when a
        # branch or a tag is created, since there is no previous commit.
        if start == NULL_SHA:
            logging.info(
                f"Start commit is null SHA ({NULL_SHA}), treating as no start commit."
            )
            start = None

        # A git failure (a compiled binary run over a checkout git rejects as
        # "dubious ownership", a remote that cannot be reached) degrades to
        # "range unknown" instead of crashing the whole `metadata` command.
        try:
            if not end or not fetch_commit(end):
                logging.warning(
                    "Skipping metadata extraction of the range of new commits."
                )
                return None

            if not start:
                logging.warning("No start commit found. Only one commit in range.")
                return (get_commit(end),)

            if not (fetch_commit(start) and deepen_until_related(start, end)):
                logging.warning(
                    "Skipping metadata extraction of the range of new commits."
                )
                return None

            # The `start..end` range already excludes `start`, so every returned
            # commit is a new one, in chronological order (oldest first).
            return list_commits(start, end)
        except GitError as error:
            logging.warning(f"git failed while resolving new commits: {error}")
            return None

    @cached_property
    def new_commits_matrix(self) -> Matrix | None:
        """Pre-computed matrix with long and short SHA values of new commits."""
        if not self.new_commits:
            return None
        return commit_matrix(tuple(commit.hash for commit in self.new_commits))

    @cached_property
    def new_commits_hash(self) -> tuple[str, ...] | None:
        """List all hashes of new commits."""
        if not self.new_commits:
            return None
        return tuple(commit.hash for commit in self.new_commits)

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
        if not self.release_commits:
            return None
        return commit_matrix(tuple(commit.hash for commit in self.release_commits))

    @cached_property
    def release_commits_hash(self) -> tuple[str, ...] | None:
        """List all hashes of release commits."""
        if not self.release_commits:
            return None
        return tuple(commit.hash for commit in self.release_commits)
