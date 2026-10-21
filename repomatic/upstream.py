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

"""The identity of the upstream toolkit, as downstream repositories see it.

A downstream repository reaches `kdeldycke/repomatic` through the `uses:` refs
of its workflows, the inline self-pin of its `run:` commands, and the paths the
canonical workflows filter on. Every writer and checker of those references
reads the names here, so a rename of the project is a change to this module.

A leaf module: it imports only {mod}`repomatic.github.urls`, which imports
nothing of the package, so any module can use it without an import cycle.
"""

from __future__ import annotations

from pathlib import Path

from .github.urls import repo_name

DEFAULT_REPO: str = "kdeldycke/repomatic"
"""Default upstream repository for reusable workflows."""

INSTALL_GUIDE_PATH = "docs/install.md"
"""Install guide the release freeze pins download URLs in.

Shared by {class}`~repomatic.release.prepare_release.PrepareRelease`, which
rewrites those URLs, and {func}`~repomatic.lint_repo.repo.check_install_guide_downloads`,
which verifies the release they name actually carries the files.
"""

UPSTREAM_ASSET_GLOB: str = ".claude/**"
"""Path glob for the bundled skills and agents in canonical workflows.

The package ships them through symlinks under `repomatic/data/`, so editing one
changes no path {data}`UPSTREAM_SOURCE_GLOB` matches, while the test suite reads
every one. Downstream, `.claude/` holds synced copies that no test reads, so the
glob is dropped there like the {data}`UPSTREAM_SOURCE_PREFIX` paths.
"""

UPSTREAM_PACKAGE: str = repo_name(DEFAULT_REPO)
"""Distribution name of the upstream toolkit, derived from {data}`DEFAULT_REPO`.

The freeze, cooldown-exemption, and lint code that handles the `uses:` refs
and the inline self-pin all key on this name: deriving it here keeps the
writer/checker pairs in lockstep and makes a rename a one-line change. It is
also the name of the import package, which {func}`is_source_repo` looks for.
"""

UPSTREAM_REPO_SLUGS: tuple[str, ...] = (
    DEFAULT_REPO,
    "kdeldycke/repokit",
    "kdeldycke/workflows",
)
"""Upstream repository slugs across the project's renames, current first.

A downstream thin-caller's `uses:` line references whichever slug was current
when it was generated. Workflow-tombstone detection matches against all of
them (current first, since most callers are recent) so an orphaned thin-caller
is recognized regardless of which era set it up."""

UPSTREAM_SOURCE_GLOB: str = f"{UPSTREAM_PACKAGE}/**"
"""Path glob for the upstream source directory in canonical workflows.

Canonical workflow `paths:` filters use this glob to match source code
changes. In downstream repos, this is replaced with the project's own source
directory.
"""

UPSTREAM_SOURCE_PREFIX: str = f"{UPSTREAM_PACKAGE}/"
"""Path prefix for upstream-specific files in canonical workflows.

Paths starting with this prefix (but not matching
{data}`UPSTREAM_SOURCE_GLOB`) are dropped in downstream thin callers because
they reference files that only exist in the upstream repository (like
`repomatic/data/labels.toml`).
"""


def is_awesome_repo(name: str) -> bool:
    """Whether a repository name marks an `awesome-*` curated list.

    The one spelling of the prefix test: repository-trait detection, the
    broken-links label choice and the awesome-template auto-inclusion all
    classify on it.
    """
    return name.startswith("awesome-")


def is_source_repo(root: Path) -> bool:
    """Whether *root* is a checkout of the upstream source repository.

    Only the upstream repository holds the package source tree, its
    `__init__.py` and its `data/` folder. `repomatic init` reads this to never
    flag the bundled files of that tree as stale, and the pin updaters to
    leave the in-tree targets of those files to their own sync.

    ```{note}
    Detection reads the contents of *root*, not `__file__`: `uvx --from .`
    installs the package into a temporary virtual environment, where `__file__`
    no longer points into the source checkout.
    ```

    :param root: Root directory of the repository to probe.
    :return: `True` for a checkout of the upstream source repository.
    """
    package = root.resolve() / UPSTREAM_PACKAGE
    return (package / "__init__.py").exists() and (package / "data").is_dir()
