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

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Final

TEST_RUNNERS_FULL = (
    "ubuntu-26.04-arm",
    "ubuntu-26.04",
    "macos-26",
    "macos-26-intel",
    "windows-11-arm",
    "windows-2025",
)
"""GitHub-hosted runners for the full test matrix.

Two variants per platform (one per architecture). See
[available images](https://github.com/actions/runner-images#available-images).

```{note} Preview images are adopted on measurement, not on GitHub's label
GitHub still marks the Ubuntu 26.04 pair *preview*, which gates their
eligibility to sit behind the `-latest` aliases. This project never uses those
aliases (a floating alias re-points with no commit to review, which
{func}`~repomatic.lint_repo.check_runner_images` rejects outright), so that
distinction does not reach it. An image is treated as stable here once it has
been validated against this suite, not once a vendor relabels it. Measured over
consecutive runs before the swap, `ubuntu-26.04-arm` beat `ubuntu-24.04-arm` by
16% on Python 3.10 and 28% on 3.14, tied on 3.15, and failed nothing.

The residual risk is capacity rather than correctness: GitHub warns a preview
image's capacity "will be balanced only throughout the next weeks", so queue
time may be worse than the runtimes above suggest. Release binaries are built
on GA images for that reason, see {data}`~repomatic.binary.NUITKA_BUILD_TARGETS`.
```

```{note} Architecture speed is not uniform across platforms
When reducing to one runner per OS, choose by measured speed, not architecture
(see {doc}`/test-matrix`). Tendencies from `repomatic`'s own full test suite:
ARM Linux runs two to three times as fast as the lean x86 `ubuntu-slim` that
preceded `ubuntu-26.04` on this axis; Apple-silicon `macos-26` beats
`macos-26-intel` by ~2x; the two Windows images tie on compute (`windows-2025`
is the PR pick). Per-job wall-clock folds in setup and upload, so isolate the
test steps before blaming the image. These figures drift as images are
re-provisioned, so re-confirm against your own job timings.
```
"""

NON_MATRIX_RUNNERS = ("ubuntu-slim",)
"""Images used by jobs that are not test-matrix cells.

```{note} Currently unused, pending an A/B
Every job has been pointed at `ubuntu-26.04` to measure the lean image against
a full one on the real mechanical workload, so nothing runs on `ubuntu-slim`
right now. The entry stays because it is the revert target, and because a
known-but-unused image costs nothing: `lint-repo` only flags an image nobody
weighed. Drop it and this whole set once the numbers settle, or restore the
jobs to it. See {doc}`/test-matrix`.
```

The test axes above answer "where is the suite exercised". This answers "what
else may a job legitimately run on", and the two are deliberately kept as close
as possible: every distinct image is one more thing to track, to pin, and to
migrate next time, so the fleet carries as little variety as it can.

Exactly one image earns a place outside the test axes. `ubuntu-slim` is the lean
default for every light mechanical job (linters and formatters), which are
setup-bound rather than compute-bound, so a faster image buys them nothing and
the small image wins on checkout. It also carries no version in its name because
GitHub rebases it onto a newer Ubuntu on its own schedule, so it never needs
migrating by hand.

Everything else, the Linux Nuitka build hosts included, runs on a test axis.
Building a published binary on the same image the suite is validated against is
one fewer variable, and the build reads its toolchain from a digest-pinned
manylinux container, so the host cannot reach the artifact either way. See
{data}`~repomatic.binary.NUITKA_BUILD_TARGETS`.

{data}`~repomatic.lint_repo.KNOWN_RUNNERS` is the union of this and the test
axes, so a job naming either is not flagged as untracked while a genuine typo
still is.
"""

TEST_RUNNERS_PR = (
    "ubuntu-26.04-arm",
    "macos-26",
    "windows-2025",
)
"""Reduced runner set for pull request test matrices.

One runner per platform: ARM Linux (`ubuntu-26.04-arm`) and Apple-silicon macOS
(`macos-26`) are the fastest of their platform on the test workload, plus x86
Windows (`windows-2025`, where the two Windows images tie on compute). x86 Linux
stays covered by the full matrix ({data}`TEST_RUNNERS_FULL`).

```{note} Why ARM Linux for the PR slot
The suite runs `pytest --numprocesses=auto`, so it scales with cores and favors
ARM, by two to three times over the lean x86 image, for quicker PR feedback.
That ratio is the heavy test suite's, not a portable property: setup-bound light
jobs (which run prebuilt single-threaded binaries) barely move between runners,
so they keep the lean `ubuntu-slim` default ({data}`NON_MATRIX_RUNNERS`). See
{doc}`/test-matrix` for the measurements.
```
"""

TEST_PYTHON_FULL = (
    "3.10",
    "3.14",
    "3.15",
)
"""Python versions tested across every runner in the full matrix.

Spans the supported range: the floor (`3.10`), the latest stable release
(`3.14`), and the in-development version (`3.15`, flagged `continue-on-error`
via {data}`UNSTABLE_PYTHON_VERSIONS`). Intermediate releases (3.11, 3.12, 3.13)
are skipped to reduce CI load. Released build *flavors* (free-threaded) are not
full-spread; they get a single-runner smoke test instead, see
{data}`SINGLE_RUNNER_PYTHON_VERSIONS`.
"""

TEST_PYTHON_PR = (
    "3.10",
    "3.14",
)
"""Reduced Python version set for pull request test matrices.

Just the floor and the latest stable release, for fast PR feedback. The
in-development version and released build flavors (free-threaded) are left to
the full matrix.
"""

UNSTABLE_PYTHON_VERSIONS: Final[frozenset[str]] = frozenset({"3.15"})
"""Python versions still in development.

Jobs using these versions run with `continue-on-error` in CI. Contrast with
{data}`SINGLE_RUNNER_PYTHON_VERSIONS`, which are released and run stable.
"""

SINGLE_RUNNER_PYTHON_VERSIONS: Final[dict[str, str]] = {"3.14t": "ubuntu-26.04-arm"}
"""Released Python build flavors smoke-tested on a single runner, mapped to it.

A free-threaded build (the `t` suffix, made officially supported in 3.14 by
[PEP 779](https://peps.python.org/pep-0779/)) runs the same released interpreter
as its base version, just without the GIL. The base version already gets the
full cross-platform spread ({data}`TEST_PYTHON_FULL`), so the library logic is
covered everywhere; the flavor only needs one runner to catch a
free-threading-specific break. These run *stable* (expected to pass), unlike the
unreleased {data}`UNSTABLE_PYTHON_VERSIONS`. The runner is `ubuntu-26.04-arm`,
the default single-runner pick: the fastest measured on compute-bound parallel
work and the cheapest tier, and free-threading targets server workloads where
Linux/ARM is the norm (see {doc}`/test-matrix`).
"""
