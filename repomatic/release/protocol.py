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

"""The release-commit protocol, and the sets that gate CI work on it.

The release machinery writes commits and branches with fixed names, and the
workflows read those names back to decide what to run. This module holds both
sides of that contract: the commit-message prefixes and the branch names the
machinery writes, and the path and branch sets that skip the binary build.

`tests/test_workflows.py` holds the workflow gates to these values.
"""

from __future__ import annotations

import re

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Final

CHANGELOG_COMMIT_PREFIX: Final[str] = "[changelog] "
"""Marker prefix carried by every machine-authored version-machinery commit.

The one bracketed prefix commit messages may carry: release freezes,
post-release bumps and manual version bumps all start with it, so a workflow
can skip machinery pushes with a single
`startsWith(github.event.head_commit.message, '[changelog] ')` clause instead
of enumerating each message shape. Conformance tests in
`tests/test_workflows.py` hold every member of
{data}`VERSION_BUMP_COMMIT_PREFIXES`, {data}`RELEASE_COMMIT_PATTERN`, the
`bump-version` template title, and the workflow gates to this prefix.
"""

RELEASE_COMMIT_PREFIX: Final[str] = f"{CHANGELOG_COMMIT_PREFIX}Release"
"""Head-commit-message prefix marking a push that carries the release commit.

The coarser sibling of {data}`RELEASE_COMMIT_PATTERN`: where that one
validates and extracts a version, this is the prefix test every workflow's
`cancel-in-progress` gate performs, so a release run is never cancelled by a
later push entering its concurrency group. A prefix is deliberately weaker
than the full pattern here, because the question is "does this push carry a
release" rather than "which version is it", and answering it must not depend
on the version number parsing.

{func}`repomatic.github.runs.cancel_superseded_runs` applies the same test
from the API side, which is the half GitHub's own concurrency mechanism
cannot cover: a manual sweep of a branch's live runs enters no concurrency
group at all.
"""

RELEASE_COMMIT_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^{re.escape(RELEASE_COMMIT_PREFIX)} v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
)
"""Pre-compiled regex for release commit messages.

Matches the full message and captures the version number. Use `fullmatch`
to validate a commit is a release commit, or `match`/`search` with
`.group("version")` to extract the version string.

A rebase merge preserves the original commit messages, so release commits
match this pattern. A squash merge replaces them with the PR title
(like ``Release `v1.2.3` (#42)``), which does **not** match. This mismatch
is the mechanism by which squash merges are safely skipped: the `create-tag`
job only processes commits matching this pattern, so no tag, PyPI publish, or
GitHub release is created from a squash merge. The `detect-squash-merge`
job in `release.yaml` detects this and opens an issue to notify the
maintainer.
"""

VERSION_BUMP_BRANCHES: Final[frozenset[str]] = frozenset((
    "major-version-increment",
    "minor-version-increment",
    "prepare-release",
))
"""PR branches that carry only automated version-bump and lockfile churn.

Members are bot-authored draft PRs created by the `bump-version` and
`prepare-release` jobs in `changelog.yaml`. Their working tree is
byte-identical to `main` except for the version string in
`pyproject.toml`, `**/__init__.py`, `changelog.md`, `citation.cff`,
and `uv.lock`. Heavy PR-time workflows (`tests.yaml`, `lint.yaml`,
`labels.yaml`) list these branches under `pull_request.branches-ignore`
so the matrix doesn't burn CI minutes for a guaranteed-passing run.

```{note}
These branches are *not* binary-neutral: the rewritten version string
is baked into the Nuitka binary, so they are deliberately absent from
{data}`SKIP_BINARY_BUILD_BRANCHES`. Post-merge release artifacts on `main`
are still produced.
```
"""

MANUAL_VERSION_BUMP_COMMIT_PREFIXES: Final[frozenset[str]] = frozenset((
    f"{CHANGELOG_COMMIT_PREFIX}Bump major version to ",
    f"{CHANGELOG_COMMIT_PREFIX}Bump minor version to ",
))
"""Head-commit-message prefixes for user-initiated version bumps.

Members are the `bump-version` job's
`[changelog] Bump $part version to \\`v$version\\`` commit messages (rendered
from the `bump-version` template's title), carrying
{data}`CHANGELOG_COMMIT_PREFIX` like every other version-machinery commit.
These merges land as a single commit on `main` and carry no other payload, so
workflows can short-circuit on them safely.

The release-cycle prefix `[changelog] Post-release bump ` is deliberately
absent from this set because the `prepare-release` merge bundles the
post-release-bump commit with the actual release commit
(`[changelog] Release vX.Y.Z`) in a single push. Workflows that gate on
the *head* commit message (`tests.yaml`, `release.yaml::compile-binaries`)
must run on those pushes to test the release commit and build its binary,
so they consult only this subset.
"""

VERSION_BUMP_COMMIT_PREFIXES: Final[frozenset[str]] = (
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES
    | frozenset({f"{CHANGELOG_COMMIT_PREFIX}Post-release bump "})
)
"""Full set of head-commit-message prefixes that mark a version-bump push.

Combines {data}`MANUAL_VERSION_BUMP_COMMIT_PREFIXES` with the
`[changelog] Post-release bump ` prefix produced by `prepare-release`
merges. Every member starts with {data}`CHANGELOG_COMMIT_PREFIX`, so
workflows without a release-artifact dependency (`lint.yaml`,
`labels.yaml`, `autofix.yaml`) gate their `metadata` job on that single
prefix and the entire job graph skips for any push generated by the
version-bump PR family. Workflows that *do* produce release artifacts on
the same push use {data}`MANUAL_VERSION_BUMP_COMMIT_PREFIXES` instead.
"""

BINARY_AFFECTING_PATHS: Final[tuple[str, ...]] = (
    ".github/workflows/_release-engine.yaml",
    ".github/workflows/release.yaml",
    "pyproject.toml",
    "tests/",
    "uv.lock",
)
"""Path prefixes that always affect compiled binaries, regardless of the project.

{attr}`~repomatic.metadata.git.GitMetadata.binary_affecting_paths` adds the
project's own source directories to these.

The release workflow entries cover both layouts: upstream keeps the
`_release-engine.yaml` lane (which defines the Nuitka compile and binary
self-test jobs) in-repo, while downstream repos call the engine cross-repo
from their generated `release.yaml`, so a pin bump there rightly triggers a
rebuild.
"""

SKIP_BINARY_BUILD_BRANCHES: Final[frozenset[str]] = frozenset((
    # Autofix branches that don't affect compiled binaries.
    "format-images",
    "format-json",
    "format-markdown",
    "format-shell",
    "sync-gitignore",
    "sync-mailmap",
    "update-dep-graph",
))
"""Autofix branches whose changes cannot affect compiled binaries.

Members are PR branch names produced by autofix jobs that touch only
repository housekeeping (`.mailmap`, `.gitignore`, JSON, Markdown,
images, shell scripts, dependency graph). The binary output is
unchanged, so {attr}`~repomatic.metadata.git.GitMetadata.skip_binary_build`
returns `True` when the PR head branch matches a member, saving an
expensive Nuitka compilation.

```{note}
This set is intentionally disjoint from {data}`VERSION_BUMP_BRANCHES`:
version-bump branches do change binaries (they rewrite the version string
baked into the build), so they belong to a different policy.
```
"""
