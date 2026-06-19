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

from __future__ import annotations

import itertools
import json
import logging

from boltons.dictutils import FrozenDict
from boltons.iterutils import unique

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


RESERVED_MATRIX_KEYWORDS = ["include", "exclude"]


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

    This Matrix behave like a `dict` and works everywhere a `dict` would. Only that
    it is immutable and based on {class}`FrozenDict`. If you want to populate the matrix
    you have to use the following methods:

    - {meth}`add_variation`
    - {meth}`add_includes`
    - {meth}`add_excludes`

    The implementation respects the order in which items were inserted. This provides a
    natural and visual sorting that should ease the inspection and debugging of large
    matrix.
    """

    def __init__(self, *args, **kwargs) -> None:
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
        """Returns all combinations and apply `include` and `exclude` constraints.

        ```{caution}
        As per GitHub specifications, all `include` combinations are processed
        after `exclude`. This allows you to use `include` to add back
        combinations that were previously excluded.
        ```

        ```{note}
        This is repomatic's own model of matrix expansion, for local inspection
        and job counting. The authoritative expansion is GitHub's: downstream
        workflows consume the serialized `variations` + `include` + `exclude`
        dict from {meth}`matrix` and let GitHub expand it. The two can diverge
        on include/exclude edge cases (notably whether an `include` resurrects
        an excluded combination), so treat `solve()` as an approximation, not a
        guarantee of GitHub's output.
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

        applicable_includes = []
        leftover_includes: list[dict[str, str]] = []

        # The matrix is empty, none of the include directive will match, so condider all
        # directives as un-applicable.
        if not self.variations:
            leftover_includes = list(self.include)

        # Search for include directives that matches the original matrix variations
        # without overwriting their values. Keep the left overs on the side.
        else:
            original_variations = self.all_variations()
            for include in self.include:
                # Keys shared between the include directive and the original matrix.
                keys_overlap = set(include).intersection(original_variations)
                # Collect include directives applicable to the original matrix.
                if (
                    # If all overlapping keys in the directive exactly match any value
                    # of the original matrix, then we are certain the directive can be
                    # applied without overwriting the original variations.
                    all(include[k] in original_variations[k] for k in keys_overlap)
                    # Same if no keys are shared, in which case these extra variations
                    # will be added to all original ones.
                    or not keys_overlap
                ):
                    applicable_includes.append(include)
                # Other directives are considered non-applicable and will be returned
                # as-is at the end of the process.
                else:
                    leftover_includes.append(include)

        # Iterates through all the variations of the original matrix, and act on the
        # matching exclude and include directives.
        for base_variations in self.product():
            # Skip the variation if it is fully matching at least one exclude directive.
            exclusion_candidate = False
            if any(
                all(
                    exclude[k] == base_variations[k]
                    for k in set(exclude).intersection(base_variations)
                )
                for exclude in self.exclude
            ):
                exclusion_candidate = True

            # Expand and/or extend the original variation set with applicable include
            # directives.
            updated_variations = base_variations.copy()
            for include in applicable_includes:
                # Check if the include directive is completely disjoint to the
                # variations of the original matrix. If that's the case, then we are
                # supposed to augment the current variation with this include, at it has
                # already been identified as applicable. But only do that if the updated
                # variation has not been already updated with a previously evaluated,
                # more targeted include directive.
                if set(include).isdisjoint(base_variations):
                    if set(include).isdisjoint(updated_variations):
                        updated_variations.update(include)
                    continue

                # Expand the base variation set with the fully matching include
                # directive.
                if all(
                    include[k] == base_variations[k]
                    for k in set(include).intersection(base_variations)
                ):
                    # Re-instate the variation set as a valid candidate since we found
                    # an include directive that is explicitly referring to it,
                    # resurrecting it from the dead.
                    exclusion_candidate = False
                    updated_variations.update(include)

            if not exclusion_candidate:
                self._count_job()
                yield updated_variations

        # Return as-is all the includes that were not applied to the original matrix.
        for variation in leftover_includes:
            self._count_job()
            yield variation
