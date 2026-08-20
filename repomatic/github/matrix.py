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

from boltons.dictutils import FrozenDict
from boltons.iterutils import unique

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence


PIVOT_CELL_SEPARATOR = ", "
"""Joins the distinct states {meth}`Matrix.pivot` finds at one intersection.

Exposed so a caller rendering the grid can split a cell back into its states
and decorate each one, instead of hard-coding the separator on its side and
letting the two drift.
"""

RESERVED_MATRIX_KEYWORDS = ("include", "exclude")
"""Keys GitHub reserves inside a `strategy.matrix` block.

Neither can name a variation axis, since both already mean something to the
matrix expander. {meth}`Matrix._check_ids` rejects them.
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
    {attr}`repomatic.metadata.Metadata.stale_test_matrix_excludes`).
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

    {meth}`matrix` renders the result as an immutable {class}`FrozenDict` for
    serialization, and {meth}`__getitem__` reads a single axis, but the object
    itself is not a mapping: it holds axes, includes and excludes as separate
    state.

    The implementation respects the order in which items were inserted. This provides a
    natural and visual sorting that should ease the inspection and debugging of large
    matrix.
    """

    def __init__(self) -> None:
        self.variations: dict[str, tuple[str, ...]] = {}

        # Tuples are used to keep track of the insertion order and force immutability.
        self.include: tuple[dict[str, str], ...] = ()
        self.exclude: tuple[dict[str, str], ...] = ()

        self._job_counter: int = 0

    def matrix(
        self, ignore_includes: bool = False, ignore_excludes: bool = False
    ) -> FrozenDict[str, tuple[str, ...] | tuple[dict[str, str], ...]]:
        """Returns a copy of the matrix.

        The special `include` and `excludes` directives will be added by default.
        You can selectively ignore them by passing the corresponding boolean parameters.
        """
        dict_copy = self.variations.copy()
        if not ignore_includes and self.include:
            dict_copy["include"] = self.include  # type: ignore[assignment]
        if not ignore_excludes and self.exclude:
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
        self._check_ids(variation_id)
        if not values:
            raise ValueError(f"No variation values provided: {values}")
        if any(type(v) is not str for v in values):
            raise ValueError(f"Only strings are accepted in {values}")
        # Extend variation with values, and deduplicate them along the way.
        var_values = list(self.variations.get(variation_id, [])) + list(values)
        self.variations[variation_id] = tuple(unique(var_values))

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
            noop_key = None
            for key, value in exclude.items():
                if key not in self.variations:
                    noop_key = key
                    break
                if value not in self.variations[key]:
                    noop_key = key
                    break
            if noop_key:
                noops.append((exclude, noop_key))
            else:
                effective.append(exclude)
        for exclude, noop_key in noops:
            logging.warning(
                "Dropping no-op exclude %s: %r is not in the %r axis.",
                exclude,
                exclude[noop_key],
                noop_key,
            )
        self.exclude = tuple(effective)

    def all_variations(
        self,
        with_matrix: bool = True,
        with_includes: bool = False,
        with_excludes: bool = False,
    ) -> dict[str, tuple[str, ...]]:
        """Collect all variations encountered in the matrix.

        Extra variations mentioned in the special `include` and `exclude`
        directives will be ignored by default.

        You can selectively expand or restrict the resulting inventory of variations by
        passing the corresponding `with_matrix`, `with_includes` and
        `with_excludes` boolean filter parameters.
        """
        all_variations = {}
        if with_matrix:
            all_variations = {k: list(v) for k, v in self.variations.items()}

        for expand, directives in (
            (with_includes, self.include),
            (with_excludes, self.exclude),
        ):
            if expand:
                for value in directives:
                    for k, v in value.items():
                        all_variations.setdefault(k, []).append(v)

        return {k: tuple(unique(v)) for k, v in all_variations.items()}

    def product(
        self, with_includes: bool = False, with_excludes: bool = False
    ) -> Iterator[dict[str, str]]:
        """Only returns the combinations of the base matrix by default.

        You can optionally add any variation referenced in the `include` and
        `exclude` special directives.

        Respects the order of variations and their values.
        """
        all_variations = self.all_variations(
            with_includes=with_includes, with_excludes=with_excludes
        )
        if not all_variations:
            return
        yield from map(
            dict,
            itertools.product(
                *(
                    tuple((variant_id, v) for v in variations)
                    for variant_id, variations in all_variations.items()
                )
            ),
        )

    def _count_job(self) -> None:
        self._job_counter += 1
        if self._job_counter > 256:
            logging.critical("GitHub job matrix limit of 256 jobs reached")

    def solve(self, strict: bool = False) -> Iterator[dict[str, str]]:
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
        """
        # GitHub jobs fails with the following message if the exclude directive is
        # referencing keys that are not present in the original base matrix:
        #   Invalid workflow file: .github/workflows/tests.yaml#L48
        #   The workflow is not valid.
        #   .github/workflows/tests.yaml (Line: 48, Col: 13): Matrix exclude key 'state'
        #   does not match any key within the matrix
        if strict:
            unreferenced_keys = set(
                self.all_variations(
                    with_matrix=False, with_includes=True, with_excludes=True
                )
            ).difference(self.variations)
            if unreferenced_keys:
                raise ValueError(
                    f"Matrix exclude keys {list(unreferenced_keys)} does not match any "
                    f"{self.variations.keys()} key within the matrix"
                )

        # Reset the number of combinations.
        self._job_counter = 0

        # Keys that come from the base cross-product. An include may not
        # overwrite these on a combination, but may freely set any other key.
        original_keys = set(self.all_variations())

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

        for job in (*jobs, *appended):
            self._count_job()
            yield job

    def pivot(
        self,
        row_axis: str = "python-version",
        col_axis: str = "os",
        cell_key: str = "state",
        missing: str = "—",
    ) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
        """Pivot the solved matrix into a 2D grid keyed by two axes.

        Expands the matrix with {meth}`solve`, then arranges the resulting jobs
        into a grid: one row per distinct `row_axis` value, one column per
        distinct `col_axis` value. Each cell holds the job's `cell_key` value at
        that intersection (its `state`, by default), or `missing` when no job
        occupies it (an excluded combination).

        :param row_axis: Job key whose values become grid rows.
        :param col_axis: Job key whose values become grid columns.
        :param cell_key: Job key whose value fills each cell.
        :param missing: Placeholder for an empty (row, col) intersection.
        :return: A `(col_values, rows)` pair. `col_values` is the ordered tuple
            of column values (distinct `col_axis` values). Each entry in `rows`
            is `(row_value, cell, …)`, with one cell per `col_values` entry.

        ```{note}
        Axis values keep first-seen order in the solved job stream. That matches
        the declared axis order for a base cross-product matrix, and the emitted
        job order for a flattened `full-include` matrix.

        When several jobs share one (row, col) intersection (a matrix carrying
        extra axes, such as a `click-version` variation), their distinct
        `cell_key` values are joined with {data}`PIVOT_CELL_SEPARATOR`. A matrix
        with only the `os` and `python-version` axes has exactly one job per
        cell.
        ```
        """
        jobs = [job for job in self.solve() if row_axis in job and col_axis in job]
        col_values = tuple(unique(job[col_axis] for job in jobs))
        row_values = tuple(unique(job[row_axis] for job in jobs))

        cells: dict[tuple[str, str], list[str]] = {}
        for job in jobs:
            cells.setdefault((job[row_axis], job[col_axis]), []).append(
                job.get(cell_key, "")
            )

        rows: list[tuple[str, ...]] = []
        for row in row_values:
            cells_in_row = []
            for col in col_values:
                states = cells.get((row, col))
                cells_in_row.append(
                    PIVOT_CELL_SEPARATOR.join(unique(states)) if states else missing
                )
            rows.append((row, *cells_in_row))

        return col_values, tuple(rows)
