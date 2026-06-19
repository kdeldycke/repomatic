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

"""Test matrix constants for CI workflows.

Defines the GitHub-hosted runner images and Python versions used to build
test matrices. Separating these from
{mod}`repomatic.metadata` makes the CI matrix configuration self-contained
and easier to update when runner images or Python releases change.
"""

from __future__ import annotations

from typing import Final

TEST_RUNNERS_FULL = (
    "ubuntu-24.04-arm",
    "ubuntu-slim",
    "macos-26",
    "macos-26-intel",
    "windows-11-arm",
    "windows-2025",
)
"""GitHub-hosted runners for the full test matrix.

Two variants per platform (one per architecture). See
[available images](https://github.com/actions/runner-images#available-images).

```{note} Architecture speed is not uniform across platforms
When reducing to one runner per OS, choose by measured speed, not architecture
(see {doc}`/test-matrix`). Tendencies observed across a full test suite: ARM
Linux (`ubuntu-24.04-arm`) runs roughly twice as fast as the lean x86
`ubuntu-slim`; Apple-silicon `macos-26` beats `macos-26-intel`; but x86
`windows-2025` beats `windows-11-arm` on recent Python. macOS is the slowest
tier overall and tends to gate the full matrix's wall-clock. These figures
drift as images are re-provisioned, so re-confirm against your own job timings.
```
"""

TEST_RUNNERS_PR = (
    "ubuntu-slim",
    "macos-26",
    "windows-2025",
)
"""Reduced runner set for pull request test matrices.

One runner per platform, skipping redundant architecture variants. The macOS
and Windows picks are the faster architecture of each (Apple silicon and x86;
see {data}`TEST_RUNNERS_FULL`).

```{note} The Linux pick trades speed for leanness
`ubuntu-slim` is the lean default image (also `repomatic`'s default for light
jobs), but it runs a full test suite roughly half as fast as
`ubuntu-24.04-arm`. Switching this Linux slot to `ubuntu-24.04-arm` would give
faster PR feedback, at the cost of exercising PRs on ARM rather than x86 Linux
(x86 stays covered in the full matrix). Left as the lean default pending a
deliberate speed-versus-cost call; see {doc}`/test-matrix`.
```
"""

TEST_PYTHON_FULL = (
    "3.10",
    "3.14",
    "3.14t",
    "3.15",
)
"""Python versions for the full test matrix.

Intermediate versions (3.11, 3.12, 3.13) are skipped to reduce CI load.
"""

TEST_PYTHON_PR = (
    "3.10",
    "3.14",
)
"""Reduced Python version set for pull request test matrices.

Skips experimental versions (free-threaded, development) to reduce CI load.
"""

UNSTABLE_PYTHON_VERSIONS: Final[frozenset[str]] = frozenset({"3.15"})
"""Python versions still in development.

Jobs using these versions run with `continue-on-error` in CI.
"""


MYPY_VERSION_MIN: Final = (3, 8)
"""Earliest version supported by Mypy's `--python-version 3.x` parameter.

[Sourced from Mypy original implementation](https://github.com/python/mypy/blob/master/mypy/defaults.py).
"""
