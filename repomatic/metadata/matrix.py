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

"""Job-matrix construction of {class}`~repomatic.metadata.core.Metadata`.

Builds the Nuitka build matrix and the test matrices from the other
concerns' facts, and applies the repository's `[tool.repomatic]`
test-matrix configuration to them.
"""

from __future__ import annotations

import logging
import re
from functools import cached_property

from ..github.event import WorkflowEvent
from ..matrix.axes import (
    PRERELEASE_LABEL_SUFFIX,
    SINGLE_RUNNER_PYTHON_VERSIONS,
    UNSTABLE_PYTHON_VERSIONS,
    MatrixName,
    python_version_sort_key,
)
from ..matrix.model import (
    JOB_STATE_KEY,
    OS_AXIS,
    PYTHON_VERSION_AXIS,
    Matrix,
    stale_axis_values,
)
from ..matrix.runners import runner_architecture
from ..release.binary import (
    NUITKA_BUILD_TARGETS,
    binary_name,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Final

    from ..config import Config


NUITKA_CACHE_KEY: Final[str] = "nuitka_cache"
"""Job key of {attr}`MatrixMetadata.nuitka_matrix` gating the compile cache.

`true` when the compile job restores and saves the target's cache, `false`
otherwise. The release workflow compares it to the string `'true'`: a matrix
value is a string, and the string `false` is truthy in a GitHub expression.
"""

PLAIN_RELEASE_RE: Final = re.compile(r"[0-9]+(?:\.[0-9]+)+")
"""A `python-version` axis value naming a plain CPython release, like `3.15`.

A build flavor (`3.15t`) or another interpreter (`pypy3.11`) does not match.
"""


def _known_build_targets(names: list[str], kind: str) -> set[str]:
    """Keep the recognized Nuitka build targets among *names*.

    Shared by {attr}`MatrixMetadata.dev_targets` and
    {attr}`MatrixMetadata.unstable_targets`, which read two different config
    lists against the same roster and would otherwise drift on how they treat a
    name that roster does not carry.

    :param names: Target names, as configured.
    :param kind: What the list configures, for the warning naming the strays.
    :return: The subset of *names* present in
        {data}`~repomatic.release.binary.NUITKA_BUILD_TARGETS`.
    """
    targets = set(names)
    unknown = targets - set(NUITKA_BUILD_TARGETS)
    if unknown:
        logging.warning(f"Unrecognized {kind} targets: {unknown}")
    return targets & set(NUITKA_BUILD_TARGETS)


class MatrixMetadata:
    """The Nuitka build matrix and the test matrices.

    A concern mixin of {class}`~repomatic.metadata.core.Metadata`: never
    instantiated on its own, and reads sibling concerns through `self`.
    """

    if TYPE_CHECKING:
        # Sibling-concern surface read through `self`: each stub mirrors
        # the descriptor another mixin defines, so every concern type-checks
        # on its own.

        @cached_property
        def config(self) -> Config:
            """See {class}`~repomatic.metadata.project.ProjectMetadata`."""

        @cached_property
        def current_commit_matrix(self) -> Matrix | None:
            """See {class}`~repomatic.metadata.git.GitMetadata`."""

        @cached_property
        def event_type(self) -> WorkflowEvent | None:
            """See {class}`~repomatic.metadata.env.EnvironmentMetadata`."""

        @cached_property
        def release_commits_matrix(self) -> Matrix | None:
            """See {class}`~repomatic.metadata.git.GitMetadata`."""

        @cached_property
        def script_entries(self) -> list[tuple[str, str, str]]:
            """See {class}`~repomatic.metadata.project.ProjectMetadata`."""

        @cached_property
        def skip_binary_build(self) -> bool:
            """See {class}`~repomatic.metadata.git.GitMetadata`."""

    @cached_property
    def nuitka_entry_points(self) -> list[str]:
        """Entry points selected for Nuitka binary compilation.

        Reads `[tool.repomatic].nuitka.entry-points` from `pyproject.toml`.
        When empty (the default), deduplicates by callable target: keeps the
        first entry point for each unique `module:callable` pair, so alias
        entry points (like both `mpm` and `meta-package-manager` pointing to
        the same function) don't produce duplicate binaries.
        Unrecognized CLI IDs are logged as warnings and discarded.
        """
        all_cli_ids = [cli_id for cli_id, _, _ in self.script_entries]
        if not all_cli_ids:
            return []

        raw = self.config.nuitka_entry_points
        if not raw:
            # Default: first entry point per unique callable target.
            seen_targets: set[str] = set()
            unique: list[str] = []
            for cli_id, module_id, callable_id in self.script_entries:
                target = f"{module_id}:{callable_id}"
                if target not in seen_targets:
                    seen_targets.add(target)
                    unique.append(cli_id)
            return unique

        selected = []
        for cli_id in raw:
            if cli_id in all_cli_ids:
                selected.append(cli_id)
            else:
                logging.warning(
                    f"Unrecognized nuitka entry point {cli_id!r}; valid: {all_cli_ids}"
                )
        return selected or all_cli_ids[:1]

    @cached_property
    def dev_targets(self) -> set[str]:
        """Nuitka build targets compiled on ordinary (non-release) pushes.

        Reads `[tool.repomatic].nuitka.dev-targets` from `pyproject.toml`. An
        empty list disables dev builds entirely. See
        {attr}`~repomatic.config.Config.nuitka_dev_targets` for the default and
        the canary rationale.

        Unrecognized target names are logged as warnings and discarded.
        """
        return _known_build_targets(self.config.nuitka_dev_targets, "dev")

    @cached_property
    def unstable_targets(self) -> set[str]:
        """Nuitka build targets allowed to fail without blocking the release.

        Reads `[tool.repomatic].nuitka.unstable-targets` from `pyproject.toml`.
        Defaults to an empty set.

        Unrecognized target names are logged as warnings and discarded.
        """
        return _known_build_targets(self.config.nuitka_unstable_targets, "unstable")

    @cached_property
    def nuitka_matrix(self) -> Matrix | None:
        """Pre-compute a matrix for Nuitka compilation workflows.

        Crosses three axes:

        - one commit per release commit (during a release) or per new commit
          (otherwise)
        - every `[project.scripts]` entry point
        - every build target of {data}`~repomatic.release.binary.NUITKA_BUILD_TARGETS`
          (runner, platform, architecture, binary extension, and the glibc floor
          or minimum-OS version that target enforces), narrowed to the
          `[tool.repomatic] nuitka.dev-targets` canary subset on an ordinary
          push (see {attr}`dev_targets`); release commits, `schedule` and
          `workflow_dispatch` runs keep the full roster

        Each axis contributes an `include` entry carrying the extra parameters
        the compile job needs, keyed on the axis value that selects it: the
        target's runner and floors, the entry point's module and callable, and
        the commit's short SHA and version. A final pass adds one `include` entry
        per `(os, entry_point, commit)` triple naming the `bin_name` the compiled
        artifact takes, since that name depends on all three at once.

        Each target entry also carries `nuitka_cache`, telling the compile job
        whether to restore and save the target's compile cache. Release commits
        build cold, on purpose: a cache entry is writable by any code running in
        any workflow of the default branch (the cache API is not gated by
        `permissions:`), and its restore is a tar extraction into the
        workspace, so a poisoned entry could reach a signed, published,
        immutable binary. A target with no use for a cache skips it too: see
        {attr}`~repomatic.release.binary.BuildTarget.compile_cache`.

        The matrix closes with `{"state": "stable"}`, which the release workflow
        reads to decide whether a failing job blocks the release.

        `None` when {attr}`~repomatic.metadata.git.GitMetadata.skip_binary_build`
        holds, so one key tells the release workflow whether to compile.

        ```{note}
        Every value comes from {data}`~repomatic.release.binary.NUITKA_BUILD_TARGETS`
        and the project's own `pyproject.toml`, so no literal is repeated here:
        run `repomatic show-metadata nuitka_matrix` against a project to see the
        matrix it computes, or `repomatic show-test-matrix` for the test one.
        ```

        ```{note}
        Each entry point compiles through Nuitka's `--main-entry-point`, which
        needs Nuitka `4.3`. Earlier releases crashed on the option
        ([Nuitka#3879](https://github.com/Nuitka/Nuitka/issues/3879)), or built
        a binary that failed at startup when the entry point carried the name
        of its package
        ([Nuitka#4024](https://github.com/Nuitka/Nuitka/issues/4024)).
        ```
        """
        # Only produce a matrix if the project is providing CLI entry points.
        if not self.script_entries:
            return None

        # Allow projects to opt out of Nuitka compilation via pyproject.toml.
        if not self.config.nuitka_enabled:
            logging.info(
                "[tool.repomatic] nuitka.enabled is disabled."
                " Skipping binary compilation."
            )
            return None

        if self.skip_binary_build:
            return None

        # On an ordinary push, compile only the canary subset: the full fleet
        # exists to refresh the rolling dev pre-release (a draft), and its
        # compile jobs contend for the account-wide runner cap on every code
        # push. Release commits, the weekly `schedule` trigger,
        # `workflow_dispatch` and local runs keep the full roster.
        build_targets = NUITKA_BUILD_TARGETS
        if self.event_type is WorkflowEvent.push and not self.release_commits_matrix:
            build_targets = {
                target_id: target_data
                for target_id, target_data in NUITKA_BUILD_TARGETS.items()
                if target_id in self.dev_targets
            }
            if not build_targets:
                logging.info(
                    "[tool.repomatic] nuitka.dev-targets selects no target."
                    " Skipping binary compilation for this push."
                )
                return None

        matrix = Matrix()

        # Register all runners on which we want to run Nuitka builds.
        matrix.add_variation(
            OS_AXIS, tuple(target.runner for target in build_targets.values())
        )
        # Augment each "os" entry with platform-specific data. Matrix values are
        # strings, so the cache flag reads `true` or `false` like a YAML one.
        release = bool(self.release_commits_matrix)
        for build_target in build_targets.values():
            cache = build_target.compile_cache and not release
            matrix.add_includes({
                **build_target.as_matrix_entry(),
                NUITKA_CACHE_KEY: str(cache).lower(),
            })

        # `[tool.nuitka]` is not assembled here: `repomatic run nuitka` resolves
        # it at build time (the tool runner translates the section to CLI flags).

        # Filter entry points to those selected for Nuitka compilation.
        selected = set(self.nuitka_entry_points)
        for cli_id, module_id, callable_id in self.script_entries:
            if cli_id not in selected:
                continue
            # CLI ID is supposed to be unique, we'll use that as a key.
            matrix.add_variation("entry_point", [cli_id])
            matrix.add_includes({
                "entry_point": cli_id,
                "cli_id": cli_id,
                "module_id": module_id,
                "callable_id": callable_id,
                # The value of Nuitka's `--main-entry-point`: Nuitka imports
                # the module by name and calls the function, like the console
                # script does, so the entry point needs no file path and
                # builds from any project layout.
                "main_entry_point": f"{cli_id}={module_id}:{callable_id}",
            })

        # For releases, only build binaries for the release (freeze) commits. The
        # post-release bump commit doesn't need binaries — only the freeze commit
        # gets tagged and attached to the GitHub release. This halves the number of
        # expensive Nuitka builds during the release cycle (6 instead of 12).
        # For non-release pushes, only build for the HEAD commit. Binary
        # compilation is expensive (6 OS/arch combinations × Nuitka), and the
        # workflow concurrency rule already cancels older runs for non-release
        # pushes — building every commit in a multi-commit push is wasteful.
        # Package builds (build-package job) still use new_commits_matrix
        # since they're cheap.
        build_commit_matrix = self.release_commits_matrix or self.current_commit_matrix
        assert build_commit_matrix
        # Extend the matrix with a new dimension: a list of commits.
        matrix.add_variation("commit", build_commit_matrix["commit"])
        matrix.add_includes(*build_commit_matrix.include)

        # Augment each variation set of the matrix with the binary name Nuitka
        # produces. Every solved job carries the metadata the name depends on,
        # and an include keyed on the job's axis values re-attaches it.
        matrix.add_includes(
            *(
                {
                    **{key: job[key] for key in matrix.variations},
                    "bin_name": binary_name(
                        job["cli_id"], job["target"], job["current_version"]
                    ),
                }
                for job in tuple(matrix.solve())
            )
        )

        # All jobs are stable by default, unless marked otherwise by specific
        # configuration. Unstable targets outside the selected subset are
        # dropped: an include whose "os" matches no matrix combination would
        # be added by GitHub as a new, half-formed combination.
        matrix.add_includes({JOB_STATE_KEY: "stable"})
        for unstable_target in self.unstable_targets:
            if unstable_target not in build_targets:
                continue
            matrix.add_includes({
                JOB_STATE_KEY: "unstable",
                OS_AXIS: NUITKA_BUILD_TARGETS[unstable_target].runner,
            })

        return matrix

    def _apply_test_matrix_config(self, matrix: Matrix, full: bool = False) -> None:
        """Apply per-project `[tool.repomatic.test-matrix]` config to a matrix.

        :param matrix: The matrix to modify in-place.
        :param full: If `True`, also apply `variations` (extra dimension
            values) and `unstable` (continue-on-error markings). Both are added
            to the full matrix only, not the PR matrix, to keep PR CI fast and
            stable.
        """
        # Replacements first, then removals: both modify axis values in-place.
        for var_id, mapping in self.config.test_matrix.replace.items():
            for old, new in mapping.items():
                matrix.replace_variation_value(var_id, old, new)
        for var_id, values in self.config.test_matrix.remove.items():
            for value in values:
                matrix.remove_variation_value(var_id, value)
        if full:
            for var_id, values in self.config.test_matrix.variations.items():
                matrix.add_variation(var_id, values)
            # Mark matching combinations continue-on-error via a `state`
            # include. Full matrix only: in the PR matrix a non-base axis key
            # (e.g. click-version) would be added to every job and hijack it.
            for combination in self.config.test_matrix.unstable:
                matrix.add_includes({**combination, JOB_STATE_KEY: "unstable"})
        if self.config.test_matrix.exclude:
            matrix.add_excludes(*self.config.test_matrix.exclude)
        if self.config.test_matrix.include:
            matrix.add_includes(*self.config.test_matrix.include)
        # Drop excludes that became no-ops after replace/remove changed the
        # axes, so GitHub Actions does not reject the matrix. No-op user
        # excludes that are likely typos are surfaced separately by the
        # lint-repo check (see Metadata.stale_test_matrix_excludes).
        matrix.prune()

    @cached_property
    def _test_matrix_base(self) -> Matrix:
        """Full test matrix in axes form, before any `full-include` flattening.

        The cross-product of OS images and Python versions plus per-project
        variations, with includes and excludes applied. {attr}`test_matrix`
        flattens this to an explicit job list when `full-include` rows are
        configured; the stale-exclude lint check reads its axis values from
        here, so it keeps working whichever form {attr}`test_matrix` emits.
        """
        matrix = Matrix()
        matrix.add_variation(OS_AXIS, MatrixName.FULL.runners)
        matrix.add_variation(PYTHON_VERSION_AXIS, MatrixName.FULL.python_versions)
        removed_os = self.config.test_matrix.remove.get(OS_AXIS, ())
        matrix.add_includes({JOB_STATE_KEY: "stable"})
        # `python-label` is display-only: it spells the version the way pyenv and
        # actions/setup-python name a prerelease, so the job title says why the
        # cell is continue-on-error. It rides alongside the version rather than
        # replacing it because uv rejects the `-dev` form, and because downstream
        # `test-matrix` directives key on the bare version. See
        # {data}`~repomatic.matrix.axes.PRERELEASE_LABEL_SUFFIX`.
        for version in sorted(UNSTABLE_PYTHON_VERSIONS):
            matrix.add_includes({
                JOB_STATE_KEY: "unstable",
                PYTHON_VERSION_AXIS: version,
                "python-label": f"{version}{PRERELEASE_LABEL_SUFFIX}",
            })
        # Released build flavors (free-threaded) are a variant of an
        # already-broadly-covered base version, so they smoke-test stable on a
        # single runner rather than the full spread. Each is a standalone
        # include pinned to its runner (it introduces a python-version absent
        # from the axis, so it joins one runner instead of multiplying across
        # the os axis); skip it when that runner was removed.
        for version, keep_os in sorted(SINGLE_RUNNER_PYTHON_VERSIONS.items()):
            if keep_os not in removed_os:
                matrix.add_includes({
                    OS_AXIS: keep_os,
                    PYTHON_VERSION_AXIS: version,
                    JOB_STATE_KEY: "stable",
                })
        self._apply_test_matrix_config(matrix, full=True)
        return matrix

    @cached_property
    def test_matrix(self) -> Matrix:
        """Full test matrix for non-PR events.

        Combines all runner OS images and Python versions. Marks development
        Python versions as unstable so CI can use `continue-on-error`, and adds
        released build flavors (free-threaded) as stable single-runner smoke
        tests. Per-project config from `[tool.repomatic.test-matrix]` is applied
        last.

        When `[tool.repomatic.test-matrix] full-include` rows are configured,
        the matrix is emitted as a flat job list (`{"include": [...]}`) so each
        row is a standalone combination GitHub runs verbatim, rather than one
        that augments a base combo sharing its `os` and `python-version`.
        """
        base = self._test_matrix_base
        full_include = self.config.test_matrix.full_include
        if not full_include:
            return base
        # Single-key default includes (like {click-version: released}) that the
        # base matrix grants every cross-product job. Collect them first so they
        # backfill both the solved base jobs and the full-include rows below.
        defaults = {
            key: value
            for directive in base.include
            if len(directive) == 1
            for key, value in directive.items()
        }
        defaults.setdefault(JOB_STATE_KEY, "stable")
        # Solve the base cross-product to explicit jobs, then append each
        # full-include row as its own standalone job. Emitting this flat list
        # sidesteps GitHub's include augment-or-add ambiguity for rows sharing
        # an os/python with the shipped-config jobs. base.solve() also appends
        # free-threaded probe jobs from standalone includes, which GitHub never
        # augments with the defaults; backfill every cell so none emits an empty
        # click-version/cloup-version that GitHub expands to "".
        rows = [{**defaults, **cell} for cell in base.solve()]
        rows.extend({**defaults, **cell} for cell in full_include)
        flat = Matrix()
        flat.add_includes(*rows)
        return flat

    @cached_property
    def test_matrix_pr(self) -> Matrix:
        """Reduced test matrix for pull requests.

        Skips experimental Python versions and redundant architecture
        variants to reduce CI load on PRs. Per-project config excludes and
        includes from `[tool.repomatic.test-matrix]` are applied, but
        variations are not (to keep the PR matrix small).
        """
        matrix = Matrix()
        matrix.add_variation(OS_AXIS, MatrixName.PR.runners)
        matrix.add_variation(PYTHON_VERSION_AXIS, MatrixName.PR.python_versions)
        matrix.add_includes({JOB_STATE_KEY: "stable"})
        self._apply_test_matrix_config(matrix, full=False)
        return matrix

    @cached_property
    def latest_stable_python(self) -> str | None:
        """The newest released Python on the full test matrix, like `3.15`.

        For a job that wants one Python to run on rather than a matrix of
        them. Read from the `python-version` axis once the project's
        `[tool.repomatic.test-matrix]` has replaced or removed values, so a
        project that dropped a version never gets it here. The in-development
        versions ({data}`~repomatic.matrix.axes.UNSTABLE_PYTHON_VERSIONS`) and
        the build flavors (like the free-threaded `3.15t`) do not count: a
        plain release is what every user installs.

        :return: The version, or `None` when the axis holds no plain release.
        """
        axes = self._test_matrix_base.all_variations()
        releases = [
            version
            for version in axes.get(PYTHON_VERSION_AXIS, ())
            if version not in UNSTABLE_PYTHON_VERSIONS
            and PLAIN_RELEASE_RE.fullmatch(version)
        ]
        return max(releases, key=python_version_sort_key, default=None)

    def test_matrix_named(self, name: MatrixName) -> Matrix:
        """The test matrix *name* designates.

        :param name: Which of the two matrices.
        :return: {attr}`test_matrix` or {attr}`test_matrix_pr`.
        """
        return self.test_matrix if name is MatrixName.FULL else self.test_matrix_pr

    @cached_property
    def runner_arch(self) -> dict[str, str]:
        """Architecture of every runner image the full test matrix can land on.

        A repository whose suite asserts on the architecture it detects needs
        to know which of its cells are ARM. Reading it from here rather than
        restating it locally is what keeps that knowledge from going stale when
        {data}`~repomatic.matrix.axes.TEST_RUNNERS_FULL` moves to a newer image:
        a hard-coded runner list keeps matching the images it was written for,
        and the new one quietly takes whichever branch the test left as its
        default.

        Covers the `include` directives as well as the `os` axis, so the
        single-runner build-flavor cells (see
        {data}`~repomatic.matrix.axes.SINGLE_RUNNER_PYTHON_VERSIONS`) are
        described too, and the per-project `variations.os` images along with
        them. Architectures are emitted as
        [Extra Platforms](https://kdeldycke.github.io/extra-platforms) ids
        (`aarch64`, `x86_64`), matching the `arch` field of
        {data}`~repomatic.release.binary.FLAT_BUILD_TARGETS`.

        :return: Runner label to architecture id, sorted by label.
        """
        axes = self._test_matrix_base.all_variations(with_includes=True)
        return {
            label: runner_architecture(label).id
            for label in sorted(axes.get(OS_AXIS, ()))
        }

    @cached_property
    def stale_test_matrix_excludes(
        self,
    ) -> list[tuple[dict[str, str], dict[str, str]]]:
        """User `test-matrix.exclude` entries matching no full-matrix axis value.

        An exclude naming a value absent from every axis (like a renamed
        runner) can never match a combination, so `Matrix.prune()` drops it
        silently and its exclusion intent is lost. This drift is common after
        an upstream runner rename (such as `macos-15-intel` becoming
        `macos-26-intel`). The `lint-repo` check surfaces these so the drift
        fails loudly instead of silently.

        The axes come from `_test_matrix_base`, never from the emitted
        {attr}`test_matrix`: a `full-include` matrix emits as a flat job list
        whose `all_variations()` is empty, which would misreport every key of
        an entry as stale. Carrying the absent values in the result is what
        keeps the lint check from re-deriving them against the wrong matrix.

        :return: `(entry, absent_values)` pairs in config order: each
            offending exclude with the key/value pairs no axis carries.
        """
        axes = self._test_matrix_base.all_variations()
        return [
            (entry, bad)
            for entry in self.config.test_matrix.exclude
            if (bad := stale_axis_values(entry, axes))
        ]
