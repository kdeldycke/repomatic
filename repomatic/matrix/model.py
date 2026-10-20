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

"""GitHub Actions job-matrix model: variations, includes, excludes, and their
expansion into the JSON payload workflow `strategy.matrix` keys consume."""

from __future__ import annotations

import itertools
import json
import logging
from collections import Counter

from boltons.dictutils import FrozenDict
from boltons.iterutils import unique

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence


GITHUB_MATRIX_JOB_LIMIT = 256
"""Most jobs GitHub expands one `strategy.matrix` into.

GitHub refuses a matrix past this size, so {meth}`Matrix.solve` reports one
before a workflow meets the refusal.
"""

JOB_STATE_KEY = "state"
"""Job key carrying whether a cell runs as `stable` or `unstable`.

{meth}`Matrix.pivot_counts` tallies it in each cell, absent a caller's choice.
"""

OS_AXIS = "os"
"""Job key naming the runner image: the default grid column."""

PYTHON_VERSION_AXIS = "python-version"
"""Job key naming the interpreter version: the default grid row."""

RESERVED_MATRIX_KEYWORDS = ("include", "exclude")
"""Keys GitHub reserves inside a `strategy.matrix` block.

Neither can name a variation axis, since both already mean something to the
matrix expander. `Matrix._check_ids` rejects them.
"""


def stale_axis_values(
    entry: Mapping[str, str], axes: Mapping[str, Sequence[str]]
) -> dict[str, str]:
    """Return the `entry` key/value pairs absent from the matrix `axes`.

    A non-empty result means an `exclude` directive can never match a
    combination: one of its keys is not a live axis, or its value is absent
    from that axis. {meth}`Matrix.prune` drops such a directive silently,
    since GitHub rejects a matrix whose excludes name unknown keys; this is
    the predicate behind that decision, exposed so callers can also *report*
    the drift instead of only absorbing it (see
    {attr}`Metadata.stale_test_matrix_excludes
    <repomatic.metadata.matrix.MatrixMetadata.stale_test_matrix_excludes>`).
    """
    return {
        key: value
        for key, value in entry.items()
        if key not in axes or value not in axes[key]
    }


class Matrix:
    """A matrix as defined by GitHub's actions workflows.

    See GitHub official documentation on [how-to implement variations of jobs in a workflow](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/running-variations-of-jobs-in-a-workflow).

    ```{note} Why matrices are pre-computed in the `metadata` job

    GitHub Actions matrix outputs are not cumulative — the last job in a
    matrix wins ([community discussion](https://github.community/t/bug-jobs-output-should-return-a-list-for-a-matrix-job/128626)).
    This makes a matrix-based job terminal in a dependency graph: no
    downstream job can depend on its aggregated outputs.

    The workaround is a single preliminary `metadata` job that computes
    all matrices upfront. Downstream jobs depend on that job and consume
    the pre-built matrices, rather than computing them themselves.
    ```

    A matrix starts empty and is populated through its own methods, never
    through the constructor:

    - {meth}`add_variation`
    - {meth}`add_includes`
    - {meth}`add_excludes`

    {meth}`matrix` renders the result as an immutable
    {class}`~boltons.dictutils.FrozenDict` for serialization, and `__getitem__`
    reads a single axis, but the object itself is not a mapping: it holds axes,
    includes and excludes as separate state.

    The implementation respects the order in which items were inserted. This provides a
    natural and visual sorting that should ease the inspection and debugging of large
    matrix.
    """

    def __init__(self) -> None:
        self.variations: dict[str, tuple[str, ...]] = {}

        # Tuples are used to keep track of the insertion order and force immutability.
        self.include: tuple[dict[str, str], ...] = ()
        self.exclude: tuple[dict[str, str], ...] = ()

    def matrix(self) -> FrozenDict[str, tuple[str, ...] | tuple[dict[str, str], ...]]:
        """Returns a copy of the matrix, with its `include` and `exclude` directives.

        A directive list that holds nothing is left out, as GitHub expects.
        """
        dict_copy = self.variations.copy()
        if self.include:
            dict_copy["include"] = self.include  # type: ignore[assignment]
        if self.exclude:
            dict_copy["exclude"] = self.exclude  # type: ignore[assignment]
        return FrozenDict(dict_copy)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.matrix()}>"

    def __str__(self) -> str:
        """Render matrix as a JSON string."""
        return json.dumps(self.matrix())

    def __getitem__(self, key: str) -> tuple[str, ...]:
        """Returns the values of a variation by its ID."""
        if key in self.variations:
            return self.variations[key]
        raise KeyError(f"Variation {key} not found in matrix")

    @staticmethod
    def _check_ids(*var_ids: str) -> None:
        for var_id in var_ids:
            if var_id in RESERVED_MATRIX_KEYWORDS:
                raise ValueError(f"{var_id} cannot be used as a variation ID")

    def add_variation(self, variation_id: str, values: Iterable[str]) -> None:
        """Extend the axis *variation_id* with *values*, dropping duplicates.

        :raises ValueError: When *values* holds nothing, or holds something
            other than a string.
        """
        self._check_ids(variation_id)
        # Read a generator once: the checks below would otherwise consume it.
        new_values = tuple(values or ())
        if not new_values:
            raise ValueError(f"No variation values provided: {values}")
        if any(type(v) is not str for v in new_values):
            raise ValueError(f"Only strings are accepted in {new_values}")
        self.variations[variation_id] = tuple(
            unique((*self.variations.get(variation_id, ()), *new_values))
        )

    def replace_variation_value(self, variation_id: str, old: str, new: str) -> None:
        """Replace a single value within a variation axis.

        The new value takes the position of the old value. If the new value
        already exists elsewhere in the axis, the duplicate is removed by
        {func}`boltons.iterutils.unique`.

        Silently skips if the axis does not exist or does not contain the
        old value, making the operation idempotent.
        """
        if variation_id not in self.variations:
            return
        values = list(self.variations[variation_id])
        if old not in values:
            return
        values[values.index(old)] = new
        self.variations[variation_id] = tuple(unique(values))

    def remove_variation_value(self, variation_id: str, value: str) -> None:
        """Remove a single value from a variation axis.

        If the axis becomes empty after removal, it is deleted entirely.

        Silently skips if the axis does not exist or does not contain the
        value, making the operation idempotent.
        """
        if variation_id not in self.variations:
            return
        values = [v for v in self.variations[variation_id] if v != value]
        if not values:
            del self.variations[variation_id]
        else:
            self.variations[variation_id] = tuple(values)

    def _add_and_dedup_dicts(
        self, *new_dicts: dict[str, str]
    ) -> tuple[dict[str, str], ...]:
        self._check_ids(*(k for d in new_dicts for k in d))
        # Use a set to track seen dicts by their sorted items tuple.
        # This avoids repeated tuple/dict conversions in the inner loop.
        seen: set[tuple[tuple[str, str], ...]] = set()
        result: list[dict[str, str]] = []
        for d in new_dicts:
            items_tuple = tuple(sorted(d.items()))
            if items_tuple not in seen:
                seen.add(items_tuple)
                result.append(d)
        return tuple(result)

    def add_includes(self, *new_includes: dict[str, str]) -> None:
        """Add one or more `include` special directives to the matrix."""
        self.include = self._add_and_dedup_dicts(*self.include, *new_includes)

    def add_excludes(self, *new_excludes: dict[str, str]) -> None:
        """Add one or more `exclude` special directives to the matrix."""
        self.exclude = self._add_and_dedup_dicts(*self.exclude, *new_excludes)

    def prune(self) -> None:
        """Remove no-op exclude directives and log about them.

        An exclude is a no-op when it references a key that is not a
        variation axis at all, or when the key exists but the value is
        not present in that axis. Either way the exclude can never match
        any combination produced by {meth}`product`, and GitHub Actions
        rejects excludes that reference non-existent matrix keys.
        """
        effective: list[dict[str, str]] = []
        noops: list[tuple[dict[str, str], str]] = []
        for exclude in self.exclude:
            stale = stale_axis_values(exclude, self.variations)
            if stale:
                noops.append((exclude, next(iter(stale))))
            else:
                effective.append(exclude)
        for exclude, noop_key in noops:
            logging.warning(
                f"Dropping no-op exclude {exclude}: {exclude[noop_key]!r} is not in the "
                f"{noop_key!r} axis."
            )
        self.exclude = tuple(effective)

    def all_variations(self, with_includes: bool = False) -> dict[str, tuple[str, ...]]:
        """Collect every axis of the matrix, with its values.

        :param with_includes: Also collect the keys and values the `include`
            directives name, after the declared axes.
        :return: Each key mapped to its distinct values, in first-seen order.
        """
        collected = {key: list(values) for key, values in self.variations.items()}
        if with_includes:
            for directive in self.include:
                for key, value in directive.items():
                    collected.setdefault(key, []).append(value)
        return {key: tuple(unique(values)) for key, values in collected.items()}

    def product(self) -> Iterator[dict[str, str]]:
        """Yield every combination of the declared axes, before any directive.

        Respects the order of the axes and of their values.
        """
        if not self.variations:
            return
        yield from map(
            dict,
            itertools.product(
                *(
                    tuple((variant_id, v) for v in variations)
                    for variant_id, variations in self.variations.items()
                )
            ),
        )

    def solve(self) -> Iterator[dict[str, str]]:
        """Expand the matrix to explicit jobs, applying `exclude` then `include`.

        Reproduces [GitHub's documented matrix
        algorithm](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations#about-matrix-strategies):

        1. Build the cross-product of the base variations.
        2. Drop every combination matching an `exclude` directive. A directive
           matches when all of its keys equal the combination's, so a partial
           directive removes a whole slice.
        3. Process `include` directives in order. Each is merged into every
           product combination it does not conflict with (it conflicts when it
           would overwrite an original axis value). A directive merging into no
           combination is appended as a new standalone job.

        ```{note}
        `include` directives augment combinations from the base cross-product
        only, never jobs created by an earlier `include`. An excluded
        combination is resurrected solely when an `include` fully re-specifies
        it, so it merges into nothing and is appended: a partial `include` that
        augments surviving jobs does not bring excluded slices back. GitHub
        remains the authoritative expander, but this follows its documented
        rules so downstream `full-include` job lists (which {meth}`matrix`
        serializes verbatim) match what GitHub would run.
        ```

        A matrix expanding past {data}`GITHUB_MATRIX_JOB_LIMIT` jobs is logged
        as critical, since GitHub refuses to run it.
        """
        # Keys that come from the base cross-product. An include may not
        # overwrite these on a combination, but may freely set any other key.
        original_keys = set(self.variations)

        # Step 1 & 2: base cross-product minus every excluded combination. A
        # combination is excluded when it fully matches at least one directive
        # on the keys that directive specifies.
        jobs: list[dict[str, str]] = []
        for combination in self.product():
            if any(
                all(
                    exclude[k] == combination[k]
                    for k in set(exclude).intersection(combination)
                )
                for exclude in self.exclude
            ):
                continue
            jobs.append(dict(combination))

        # Step 3: apply include directives in order. Merge a directive into
        # every surviving product job whose original axis values it does not
        # overwrite; if it merges into none, append it as a new standalone job.
        # Excluded combinations are gone from `jobs`, so a partial directive can
        # no longer resurrect them, while a directive that fully re-specifies an
        # excluded combination matches nothing and is appended (GitHub's
        # documented "add back" behavior).
        appended: list[dict[str, str]] = []
        for include in self.include:
            conflict_keys = original_keys.intersection(include)
            merged = False
            for job in jobs:
                if all(include[k] == job[k] for k in conflict_keys):
                    job.update(include)
                    merged = True
            if not merged:
                appended.append(dict(include))

        solved = (*jobs, *appended)
        if len(solved) > GITHUB_MATRIX_JOB_LIMIT:
            logging.critical(
                f"The matrix expands to {len(solved)} jobs, past the GitHub limit"
                f" of {GITHUB_MATRIX_JOB_LIMIT}."
            )
        yield from solved

    def job_keys(self) -> tuple[str, ...]:
        """Every key a job of the solved matrix carries, sorted.

        Read off the solved jobs rather than off the declared axes, so a key
        only an `include` directive sets is listed, and a key whose directive
        an `exclude` cancelled is not. That makes it the set of keys
        {meth}`pivot_counts` can lay a grid out on.
        """
        return tuple(sorted({key for job in self.solve() for key in job}))

    def pivot_counts(
        self,
        row_axis: str = PYTHON_VERSION_AXIS,
        col_axis: str = OS_AXIS,
        cell_key: str = JOB_STATE_KEY,
    ) -> dict[tuple[str, str], Counter[str]]:
        """Tally, per grid intersection, the jobs carrying each `cell_key` value.

        A grid has two axes, so a matrix varying on a third lands several jobs
        on one intersection. The counts keep them apart: a cell standing for
        five jobs of one state and a cell standing for one job are told apart
        by their count.

        :param row_axis: Job key whose values become grid rows.
        :param col_axis: Job key whose values become grid columns.
        :param cell_key: Job key whose values are tallied.
        :return: A `(row_value, col_value)` to {class}`~collections.Counter`
            mapping, in first-seen job order, each counter keyed in first-seen
            order too. An intersection no job occupies is absent from the
            mapping rather than present with an empty counter.
        """
        tallies: dict[tuple[str, str], Counter[str]] = {}
        for job in self.solve():
            if row_axis not in job or col_axis not in job:
                continue
            cell = tallies.setdefault((job[row_axis], job[col_axis]), Counter())
            cell[job.get(cell_key, "")] += 1
        return tallies
