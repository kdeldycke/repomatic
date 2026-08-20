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

"""Tests for `repomatic.github.matrix`: the workflow matrix model."""

from __future__ import annotations

from collections import Counter
from itertools import permutations

import pytest

from repomatic.github.matrix import Matrix


def test_matrix_constructor_takes_no_arguments():
    """A matrix is populated through its methods, never through the constructor.

    The signature used to swallow `*args, **kwargs`, so `Matrix({"os": [...]})`
    returned an empty matrix instead of raising.
    """
    with pytest.raises(TypeError):
        Matrix({"os": ["ubuntu-slim"]})  # type: ignore[call-arg]


def test_matrix():
    """Construction, deduplicating `add_variation`, rendering, and rejections.

    `include` and `exclude` are reserved: they name the directive lists, so
    accepting them as axis names would shadow the very keys `solve` reads.
    """
    matrix = Matrix()

    assert isinstance(matrix, Matrix)
    assert not isinstance(matrix, dict)

    assert hasattr(matrix, "variations")
    assert isinstance(matrix.variations, dict)

    assert hasattr(matrix, "include")
    assert hasattr(matrix, "exclude")
    assert matrix.include == ()
    assert matrix.exclude == ()

    matrix.add_variation("foo", ["a", "b", "c"])
    assert matrix.variations == {"foo": ("a", "b", "c")}
    assert not matrix.include
    assert not matrix.exclude

    # Natural deduplication.
    matrix.add_variation("foo", ["a", "a", "d"])
    assert matrix.variations == {"foo": ("a", "b", "c", "d")}
    assert not matrix.include
    assert not matrix.exclude

    assert matrix.matrix() == {"foo": ("a", "b", "c", "d")}

    assert str(matrix) == '{"foo": ["a", "b", "c", "d"]}'
    assert repr(matrix) == "<Matrix: FrozenDict({'foo': ('a', 'b', 'c', 'd')})>"

    with pytest.raises(ValueError):
        matrix.add_variation("variation_1", None)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        matrix.add_variation("variation_1", [])

    with pytest.raises(ValueError):
        matrix.add_variation("variation_1", [None])  # type: ignore[list-item]

    with pytest.raises(ValueError):
        matrix.add_variation("include", ["a", "b", "c"])

    with pytest.raises(ValueError):
        matrix.add_variation("exclude", ["a", "b", "c"])


def test_replace_variation_value():
    """`replace` swaps one axis value for another, in place."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26", "windows-2025"])
    matrix.add_variation("version", ["3.10", "3.14"])

    # Basic replacement preserves position.
    matrix.replace_variation_value("os", "ubuntu-slim", "ubuntu-24.04")
    assert matrix.variations["os"] == ("ubuntu-24.04", "macos-26", "windows-2025")

    # Other axes are untouched.
    assert matrix.variations["version"] == ("3.10", "3.14")

    # Missing axis is a silent no-op.
    matrix.replace_variation_value("nonexistent", "a", "b")
    assert "nonexistent" not in matrix.variations

    # Missing value within an existing axis is a silent no-op.
    matrix.replace_variation_value("os", "linux-latest", "ubuntu-22.04")
    assert matrix.variations["os"] == ("ubuntu-24.04", "macos-26", "windows-2025")

    # Replacement deduplicates when the new value already exists.
    matrix.replace_variation_value("os", "windows-2025", "macos-26")
    assert matrix.variations["os"] == ("ubuntu-24.04", "macos-26")


def test_remove_variation_value():
    """`remove` drops an axis value, and the axis once it empties."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26", "windows-2025"])
    matrix.add_variation("version", ["3.10", "3.14"])

    # Basic removal.
    matrix.remove_variation_value("os", "ubuntu-slim")
    assert matrix.variations["os"] == ("macos-26", "windows-2025")

    # Other axes are untouched.
    assert matrix.variations["version"] == ("3.10", "3.14")

    # Missing axis is a silent no-op.
    matrix.remove_variation_value("nonexistent", "a")
    assert "nonexistent" not in matrix.variations

    # Missing value within an existing axis is a silent no-op.
    matrix.remove_variation_value("os", "linux-latest")
    assert matrix.variations["os"] == ("macos-26", "windows-2025")

    # Removing all values deletes the axis.
    matrix.remove_variation_value("os", "macos-26")
    matrix.remove_variation_value("os", "windows-2025")
    assert "os" not in matrix.variations


def test_prune(caplog):
    """Prune removes no-op excludes and logs about them."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-24.04", "macos-26"])
    matrix.add_variation("version", ["3.10", "3.14"])

    # Effective exclude: os value exists in the axis.
    matrix.add_excludes({"os": "macos-26", "version": "3.10"})
    # No-op exclude: os value does not exist in the axis.
    matrix.add_excludes({"os": "windows-11-arm"})
    # No-op exclude: version value does not exist in the axis.
    matrix.add_excludes({"version": "3.15t"})
    # No-op exclude: key not in any axis. GitHub Actions rejects excludes
    # referencing non-existent matrix keys, so prune must drop these too.
    matrix.add_excludes({"state": "unstable"})

    assert len(matrix.exclude) == 4

    matrix.prune()

    # Only the effective exclude remains. Reassign with an explicit
    # annotation to widen the type back to a variable-length tuple. The
    # ``assert len(...) == 4`` above narrows ``matrix.exclude`` to a
    # fixed-length 4-tuple; mypy cannot track the mutation performed by
    # ``prune()``, so without this it considers the ``len(...) == 1``
    # assert unreachable.
    exclude: tuple[dict[str, str], ...] = matrix.exclude
    assert len(exclude) == 1
    assert {"os": "macos-26", "version": "3.10"} in exclude

    assert "Dropping no-op exclude" in caplog.text
    assert "'windows-11-arm'" in caplog.text
    assert "'3.15t'" in caplog.text
    assert "'state'" in caplog.text


def test_remove_variation_value_solve():
    """Exclude prevents resurrection; removal also strips the axis value."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26", "windows-11-arm"])
    matrix.add_variation("version", ["3.14", "3.15"])
    matrix.add_includes({"state": "stable"})
    matrix.add_includes({"state": "unstable", "version": "3.15"})
    matrix.add_excludes({"os": "windows-11-arm"})

    # The partial unstable include augments the surviving 3.15 jobs; it does not
    # resurrect the excluded windows-11-arm × 3.15 combination (GitHub's
    # behavior). windows-11-arm stays a declared axis value, only its
    # combinations are excluded.
    solved_with_exclude = tuple(matrix.solve())
    assert all(j["os"] != "windows-11-arm" for j in solved_with_exclude)
    assert len(solved_with_exclude) == 4
    assert {"os": "macos-26", "version": "3.15", "state": "unstable"} in (
        solved_with_exclude
    )
    assert "windows-11-arm" in matrix.variations["os"]

    # Removal additionally strips the value from the axis entirely.
    matrix.remove_variation_value("os", "windows-11-arm")
    solved_with_remove = tuple(matrix.solve())
    assert all(j["os"] != "windows-11-arm" for j in solved_with_remove)
    assert len(solved_with_remove) == 4
    assert "windows-11-arm" not in matrix.variations["os"]


def test_solve_matches_github_include_example():
    """solve() reproduces GitHub's documented matrix include expansion.

    The canonical example from [GitHub's
    docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations#example-expanding-configurations):
    later includes augment base combinations only, never combinations a previous
    include created, and an include that matches no combination is appended.
    """
    matrix = Matrix()
    matrix.add_variation("fruit", ["apple", "pear"])
    matrix.add_variation("animal", ["cat", "dog"])
    matrix.add_includes({"color": "green"})
    matrix.add_includes({"color": "pink", "animal": "cat"})
    matrix.add_includes({"fruit": "apple", "shape": "circle"})
    matrix.add_includes({"fruit": "banana"})
    matrix.add_includes({"fruit": "banana", "animal": "cat"})
    assert tuple(matrix.solve()) == (
        {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
        {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
        {"fruit": "pear", "animal": "cat", "color": "pink"},
        {"fruit": "pear", "animal": "dog", "color": "green"},
        {"fruit": "banana"},
        {"fruit": "banana", "animal": "cat"},
    )


def test_solve_partial_include_does_not_resurrect_excluded():
    """A partial include augments survivors without resurrecting excluded combos."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26", "windows-2025"])
    matrix.add_variation("python-version", ["3.10", "3.15"])
    # Carve the 3.15 probe down to a single runner.
    matrix.add_excludes(
        {"os": "macos-26", "python-version": "3.15"},
        {"os": "windows-2025", "python-version": "3.15"},
    )
    # A partial include flags every surviving 3.15 job unstable.
    matrix.add_includes({"state": "unstable", "python-version": "3.15"})

    solved = tuple(matrix.solve())
    # Only ubuntu-slim keeps a 3.15 job; the excluded ones stay gone.
    py315 = [j for j in solved if j["python-version"] == "3.15"]
    assert py315 == [
        {"os": "ubuntu-slim", "python-version": "3.15", "state": "unstable"}
    ]
    assert len(solved) == 4


def test_solve_full_include_resurrects_excluded():
    """A fully-specified include adds back an excluded combination (GitHub behavior)."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26"])
    matrix.add_variation("python-version", ["3.10", "3.14"])
    matrix.add_excludes({"os": "macos-26", "python-version": "3.14"})
    # An include that re-specifies the excluded cell brings it back.
    matrix.add_includes({"os": "macos-26", "python-version": "3.14", "extra": "yes"})

    solved = tuple(matrix.solve())
    assert {"os": "macos-26", "python-version": "3.14", "extra": "yes"} in solved
    assert len(solved) == 4


def test_replace_variation_value_solve():
    """Replacement integrates correctly with the full solve pipeline."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26"])
    matrix.add_variation("version", ["3.10", "3.14"])
    matrix.add_includes({"state": "stable"})
    matrix.add_excludes({"os": "ubuntu-slim", "version": "3.10"})

    matrix.replace_variation_value("os", "ubuntu-slim", "ubuntu-24.04")

    # The exclude still references "ubuntu-slim", which no longer exists in the
    # axis, so the combination is no longer excluded.
    assert tuple(matrix.solve()) == (
        {"os": "ubuntu-24.04", "version": "3.10", "state": "stable"},
        {"os": "ubuntu-24.04", "version": "3.14", "state": "stable"},
        {"os": "macos-26", "version": "3.10", "state": "stable"},
        {"os": "macos-26", "version": "3.14", "state": "stable"},
    )


def test_includes():
    """`add_includes` accumulates directives without touching the axes."""
    matrix = Matrix()

    matrix.add_variation("foo", ["a", "b", "c"])
    assert matrix.matrix() == {"foo": ("a", "b", "c")}

    # First addition.
    matrix.add_includes({"foo": "a", "bar": "1"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "include": ({"foo": "a", "bar": "1"},),
    }

    # Second addition is cumulative.
    matrix.add_includes({"foo": "b", "bar": "2"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "include": ({"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}),
    }

    # Deduplication.
    matrix.add_includes({"foo": "b", "bar": "2"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "include": ({"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}),
    }

    # Rendering.
    assert (
        str(matrix) == '{"foo": ["a", "b", "c"], '
        '"include": [{"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}]}'
    )
    assert (
        repr(matrix) == "<Matrix: FrozenDict({'foo': ('a', 'b', 'c'), "
        "'include': ({'foo': 'a', 'bar': '1'}, {'foo': 'b', 'bar': '2'})})>"
    )

    # Multiple insertions.
    matrix.add_includes({"foo": "c", "bar": "3"}, {"foo": "d", "bar": "4"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "include": (
            {"foo": "a", "bar": "1"},
            {"foo": "b", "bar": "2"},
            {"foo": "c", "bar": "3"},
            {"foo": "d", "bar": "4"},
        ),
    }

    # Forbidden values.
    with pytest.raises(ValueError):
        matrix.add_includes({"include": "random"})
    with pytest.raises(ValueError):
        matrix.add_includes({"exclude": "random"})


def test_excludes():
    """`add_excludes` accumulates directives without touching the axes."""
    matrix = Matrix()

    matrix.add_variation("foo", ["a", "b", "c"])
    assert matrix.matrix() == {"foo": ("a", "b", "c")}

    # First addition.
    matrix.add_excludes({"foo": "a", "bar": "1"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "exclude": ({"foo": "a", "bar": "1"},),
    }

    # Second addition is cumulative.
    matrix.add_excludes({"foo": "b", "bar": "2"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "exclude": ({"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}),
    }

    # Deduplication.
    matrix.add_excludes({"foo": "b", "bar": "2"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "exclude": ({"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}),
    }

    # Rendering.
    assert (
        str(matrix) == '{"foo": ["a", "b", "c"], '
        '"exclude": [{"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}]}'
    )
    assert (
        repr(matrix) == "<Matrix: FrozenDict({'foo': ('a', 'b', 'c'), "
        "'exclude': ({'foo': 'a', 'bar': '1'}, {'foo': 'b', 'bar': '2'})})>"
    )

    # Multiple insertions.
    matrix.add_excludes({"foo": "c", "bar": "3"}, {"foo": "d", "bar": "4"})
    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "exclude": (
            {"foo": "a", "bar": "1"},
            {"foo": "b", "bar": "2"},
            {"foo": "c", "bar": "3"},
            {"foo": "d", "bar": "4"},
        ),
    }

    # Forbidden values.
    with pytest.raises(ValueError):
        matrix.add_excludes({"include": "random"})
    with pytest.raises(ValueError):
        matrix.add_excludes({"exclude": "random"})


def test_all_variations():
    """`all_variations` reports every axis and its values."""
    matrix = Matrix()

    assert matrix.all_variations() == {}

    matrix.add_variation("foo", ["a", "b", "c"])
    matrix.add_variation("bar", ["1", "2", "3"])

    matrix.add_includes(
        {"foo": "b", "color": "green"},
        {"foo": "d", "color": "orange"},
        {"bar": "1", "shape": "triangle"},
        {"size": "small"},
    )
    matrix.add_excludes(
        {"foo": "b", "shape": "circle"},
        {"bar": "2", "color": "blue"},
        {"bar": "4", "color": "yellow"},
        {"weight": "heavy"},
    )

    assert matrix.matrix() == {
        "foo": ("a", "b", "c"),
        "bar": ("1", "2", "3"),
        "include": (
            {"foo": "b", "color": "green"},
            {"foo": "d", "color": "orange"},
            {"bar": "1", "shape": "triangle"},
            {"size": "small"},
        ),
        "exclude": (
            {"foo": "b", "shape": "circle"},
            {"bar": "2", "color": "blue"},
            {"bar": "4", "color": "yellow"},
            {"weight": "heavy"},
        ),
    }

    assert matrix.matrix(ignore_includes=True) == {
        "foo": ("a", "b", "c"),
        "bar": ("1", "2", "3"),
        "exclude": (
            {"foo": "b", "shape": "circle"},
            {"bar": "2", "color": "blue"},
            {"bar": "4", "color": "yellow"},
            {"weight": "heavy"},
        ),
    }

    assert matrix.matrix(ignore_excludes=True) == {
        "foo": ("a", "b", "c"),
        "bar": ("1", "2", "3"),
        "include": (
            {"foo": "b", "color": "green"},
            {"foo": "d", "color": "orange"},
            {"bar": "1", "shape": "triangle"},
            {"size": "small"},
        ),
    }

    assert matrix.matrix(ignore_includes=True, ignore_excludes=True) == {
        "foo": ("a", "b", "c"),
        "bar": ("1", "2", "3"),
    }

    assert matrix.all_variations() == {
        "foo": ("a", "b", "c"),
        "bar": ("1", "2", "3"),
    }

    assert matrix.all_variations(with_includes=True) == {
        "foo": ("a", "b", "c", "d"),
        "bar": ("1", "2", "3"),
        "color": ("green", "orange"),
        "shape": ("triangle",),
        "size": ("small",),
    }

    assert matrix.all_variations(with_excludes=True) == {
        "foo": ("a", "b", "c"),
        "bar": ("1", "2", "3", "4"),
        "shape": ("circle",),
        "color": ("blue", "yellow"),
        "weight": ("heavy",),
    }

    assert matrix.all_variations(with_includes=True, with_excludes=True) == {
        "foo": ("a", "b", "c", "d"),
        "bar": ("1", "2", "3", "4"),
        "color": ("green", "orange", "blue", "yellow"),
        "shape": ("triangle", "circle"),
        "size": ("small",),
        "weight": ("heavy",),
    }

    assert matrix.all_variations(with_matrix=False) == {}
    assert (
        matrix.all_variations(
            with_matrix=False, with_excludes=False, with_includes=False
        )
        == {}
    )

    assert matrix.all_variations(with_matrix=False, with_includes=True) == {
        "foo": ("b", "d"),
        "color": ("green", "orange"),
        "bar": ("1",),
        "shape": ("triangle",),
        "size": ("small",),
    }

    assert matrix.all_variations(with_matrix=False, with_excludes=True) == {
        "foo": ("b",),
        "shape": ("circle",),
        "bar": ("2", "4"),
        "color": ("blue", "yellow"),
        "weight": ("heavy",),
    }


def test_product():
    """`product` is the plain cartesian product, before include/exclude."""
    matrix = Matrix()

    assert tuple(matrix.product()) == ()

    matrix.add_variation("foo", ["a", "b"])
    matrix.add_variation("bar", ["1", "2"])

    assert tuple(matrix.product()) == (
        {"foo": "a", "bar": "1"},
        {"foo": "a", "bar": "2"},
        {"foo": "b", "bar": "1"},
        {"foo": "b", "bar": "2"},
    )

    matrix.add_includes(
        {"foo": "b", "baz": "W"},
        {"foo": "c", "baz": "X"},
        {"bar": "1", "qux": "福"},
        {"@": "$"},
    )
    matrix.add_excludes(
        {"foo": "b", "qux": "子"},
        {"bar": "2", "baz": "Y"},
        {"bar": "3", "baz": "Z"},
        {"E": "O"},
    )

    assert tuple(matrix.product()) == (
        {"foo": "a", "bar": "1"},
        {"foo": "a", "bar": "2"},
        {"foo": "b", "bar": "1"},
        {"foo": "b", "bar": "2"},
    )

    assert tuple(matrix.product(with_includes=True)) == (
        {"foo": "a", "bar": "1", "baz": "W", "qux": "福", "@": "$"},
        {"foo": "a", "bar": "1", "baz": "X", "qux": "福", "@": "$"},
        {"foo": "a", "bar": "2", "baz": "W", "qux": "福", "@": "$"},
        {"foo": "a", "bar": "2", "baz": "X", "qux": "福", "@": "$"},
        {"foo": "b", "bar": "1", "baz": "W", "qux": "福", "@": "$"},
        {"foo": "b", "bar": "1", "baz": "X", "qux": "福", "@": "$"},
        {"foo": "b", "bar": "2", "baz": "W", "qux": "福", "@": "$"},
        {"foo": "b", "bar": "2", "baz": "X", "qux": "福", "@": "$"},
        {"foo": "c", "bar": "1", "baz": "W", "qux": "福", "@": "$"},
        {"foo": "c", "bar": "1", "baz": "X", "qux": "福", "@": "$"},
        {"foo": "c", "bar": "2", "baz": "W", "qux": "福", "@": "$"},
        {"foo": "c", "bar": "2", "baz": "X", "qux": "福", "@": "$"},
    )

    assert tuple(matrix.product(with_excludes=True)) == (
        {"foo": "a", "bar": "1", "qux": "子", "baz": "Y", "E": "O"},
        {"foo": "a", "bar": "1", "qux": "子", "baz": "Z", "E": "O"},
        {"foo": "a", "bar": "2", "qux": "子", "baz": "Y", "E": "O"},
        {"foo": "a", "bar": "2", "qux": "子", "baz": "Z", "E": "O"},
        {"foo": "a", "bar": "3", "qux": "子", "baz": "Y", "E": "O"},
        {"foo": "a", "bar": "3", "qux": "子", "baz": "Z", "E": "O"},
        {"foo": "b", "bar": "1", "qux": "子", "baz": "Y", "E": "O"},
        {"foo": "b", "bar": "1", "qux": "子", "baz": "Z", "E": "O"},
        {"foo": "b", "bar": "2", "qux": "子", "baz": "Y", "E": "O"},
        {"foo": "b", "bar": "2", "qux": "子", "baz": "Z", "E": "O"},
        {"foo": "b", "bar": "3", "qux": "子", "baz": "Y", "E": "O"},
        {"foo": "b", "bar": "3", "qux": "子", "baz": "Z", "E": "O"},
    )

    assert tuple(matrix.product(with_includes=True, with_excludes=True)) == (
        {"foo": "a", "bar": "1", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "1", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "1", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "1", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "1", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "1", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "1", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "1", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "2", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "a", "bar": "3", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "1", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "2", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "b", "bar": "3", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "1", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "2", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "W", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "W", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "X", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "X", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "Y", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "Y", "qux": "子", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "Z", "qux": "福", "@": "$", "E": "O"},
        {"foo": "c", "bar": "3", "baz": "Z", "qux": "子", "@": "$", "E": "O"},
    )


def test_solve_includes_are_order_sensitive():
    """Later includes overwrite non-axis values added by earlier ones.

    GitHub applies includes sequentially, so `{color: green}` (added to every
    combination) overwrites the `color: pink` an earlier, more specific include
    set on the cat combinations. Reversing the two would keep pink. See
    {func}`test_solve_matches_github_include_example` for the documented order.
    """
    matrix = Matrix()
    matrix.add_variation("fruit", ["apple", "pear"])
    matrix.add_variation("animal", ["cat", "dog"])
    matrix.add_includes(
        {"color": "pink", "animal": "cat"},
        {"color": "green"},
    )
    assert tuple(matrix.solve()) == (
        {"fruit": "apple", "animal": "cat", "color": "green"},
        {"fruit": "apple", "animal": "dog", "color": "green"},
        {"fruit": "pear", "animal": "cat", "color": "green"},
        {"fruit": "pear", "animal": "dog", "color": "green"},
    )


def test_solve_expanded_configuration():
    """GitHub's documented include example: a matching include expands one cell."""
    matrix = Matrix()

    matrix.add_variation("os", ["windows-latest", "ubuntu-latest"])
    matrix.add_variation("node", ["14", "16"])

    matrix.add_includes({"os": "windows-latest", "node": "16", "npm": "6"})

    assert tuple(matrix.solve()) == (
        {"os": "windows-latest", "node": "14"},
        {"os": "windows-latest", "node": "16", "npm": "6"},
        {"os": "ubuntu-latest", "node": "14"},
        {"os": "ubuntu-latest", "node": "16"},
    )


def test_solve_extended_configuration():
    """GitHub's documented include example: a non-matching include appends a job."""
    matrix = Matrix()

    matrix.add_variation("os", ["macos-latest", "windows-latest"])
    matrix.add_variation("version", ["12", "14", "16"])

    matrix.add_includes({"os": "windows-latest", "version": "17"})

    assert tuple(matrix.solve()) == (
        {"os": "macos-latest", "version": "12"},
        {"os": "macos-latest", "version": "14"},
        {"os": "macos-latest", "version": "16"},
        {"os": "windows-latest", "version": "12"},
        {"os": "windows-latest", "version": "14"},
        {"os": "windows-latest", "version": "16"},
        {"os": "windows-latest", "version": "17"},
    )


def test_solve_empty_matrix():
    """Includes alone define the jobs when no axis is declared."""
    matrix = Matrix()

    matrix.add_includes(
        {"site": "production", "datacenter": "site-a"},
        {"site": "staging", "datacenter": "site-b"},
    )

    assert tuple(matrix.solve()) == (
        {"site": "production", "datacenter": "site-a"},
        {"site": "staging", "datacenter": "site-b"},
    )


@pytest.mark.parametrize(
    "excludes",
    list(
        permutations(
            (
                # The order of these 3 includes directives can be shuffled as the
                # final result order is imposed by the base variations of the original
                # matrix.
                {"os": "macos-latest", "version": "12", "environment": "production"},
                {"os": "windows-latest", "version": "16"},
            ),
            2,
        )
    ),
)
def test_solve_excludes(excludes):
    """Excludes remove matching cells, whatever order they are declared in."""
    matrix = Matrix()

    matrix.add_variation("os", ["macos-latest", "windows-latest"])
    matrix.add_variation("version", ["12", "14", "16"])
    matrix.add_variation("environment", ["staging", "production"])

    matrix.add_excludes(*excludes)

    assert tuple(matrix.solve()) == (
        {"os": "macos-latest", "version": "12", "environment": "staging"},
        # {"os": "macos-latest", "version": "12", "environment": "production"},
        {"os": "macos-latest", "version": "14", "environment": "staging"},
        {"os": "macos-latest", "version": "14", "environment": "production"},
        {"os": "macos-latest", "version": "16", "environment": "staging"},
        {"os": "macos-latest", "version": "16", "environment": "production"},
        {"os": "windows-latest", "version": "12", "environment": "staging"},
        {"os": "windows-latest", "version": "12", "environment": "production"},
        {"os": "windows-latest", "version": "14", "environment": "staging"},
        {"os": "windows-latest", "version": "14", "environment": "production"},
        # {"os": "windows-latest", "version": "16", "environment": "staging"},
        # {"os": "windows-latest", "version": "16", "environment": "production"},
    )


@pytest.mark.parametrize(
    "excludes",
    list(
        permutations(
            (
                # The order of these 2 excludes directives can be shuffled as the
                # final result order is imposed by the base variations of the original
                # matrix.
                {"os": "windows-latest", "version": "14"},
                {"os": "linux-latest"},
            ),
            2,
        )
    ),
)
def test_solve_exclude_partial(excludes):
    """A partial exclude removes every cell sharing the named values."""
    matrix = Matrix()

    matrix.add_variation("os", ["macos-latest", "windows-latest", "linux-latest"])
    matrix.add_variation("version", ["14", "16"])

    matrix.add_excludes(*excludes)

    assert tuple(matrix.solve()) == (
        {"os": "macos-latest", "version": "14"},
        {"os": "macos-latest", "version": "16"},
        # {"os": "windows-latest", "version": "14"},
        {"os": "windows-latest", "version": "16"},
        # {"os": "linux-latest", "version": "14"},
        # {"os": "linux-latest", "version": "16"},
    )


def test_solve_exclude_include_priority():
    """A partial include re-added after exclude is a single job, not a slice.

    The include `{os: linux-latest}` matches no surviving combination (adding it
    would overwrite the original `os`), so GitHub appends it verbatim as one new
    job. It does not fan back out across the excluded `version` axis: that would
    require the include to specify each `version` itself.
    """
    matrix = Matrix()

    matrix.add_variation("os", ["macos-latest", "windows-latest", "linux-latest"])
    matrix.add_variation("version", ["14", "16"])

    matrix.add_includes({"os": "linux-latest"})
    matrix.add_excludes({"os": "linux-latest"})

    assert tuple(matrix.solve()) == (
        {"os": "macos-latest", "version": "14"},
        {"os": "macos-latest", "version": "16"},
        {"os": "windows-latest", "version": "14"},
        {"os": "windows-latest", "version": "16"},
        {"os": "linux-latest"},
    )


def test_solve_exclude_include_selectivity():
    """A fully-qualified include survives an exclude that removes its siblings."""
    matrix = Matrix()

    matrix.add_variation("os", ["macos-latest", "windows-latest", "linux-latest"])
    matrix.add_variation("version", ["14", "16"])

    matrix.add_includes({"os": "linux-latest", "version": "16", "node": "20"})
    matrix.add_excludes({"os": "linux-latest"})

    assert tuple(matrix.solve()) == (
        {"os": "macos-latest", "version": "14"},
        {"os": "macos-latest", "version": "16"},
        {"os": "windows-latest", "version": "14"},
        {"os": "windows-latest", "version": "16"},
        # {"os": "linux-latest", "version": "14"},
        {"os": "linux-latest", "version": "16", "node": "20"},
    )


def test_strict_mode_unknown_exclude_key():
    """Strict mode rejects an exclude naming an axis the matrix never declared."""
    matrix = Matrix()

    matrix.add_variation("os", ["macos-latest", "windows-latest", "linux-latest"])
    matrix.add_variation("version", ["3.14", "3.13", "3.12"])

    matrix.add_includes()
    matrix.add_excludes(
        {"os": "linux-latest", "unknown_key": "random"},
        {"version": "3.14"},
    )

    assert tuple(matrix.solve(strict=False)) == (
        {"os": "macos-latest", "version": "3.13"},
        {"os": "macos-latest", "version": "3.12"},
        {"os": "windows-latest", "version": "3.13"},
        {"os": "windows-latest", "version": "3.12"},
    )

    with pytest.raises(ValueError):
        tuple(matrix.solve(strict=True))


def test_pivot_basic():
    """pivot() lays solved jobs out as a row-axis by col-axis grid."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26"])
    matrix.add_variation("python-version", ["3.10", "3.14"])
    matrix.add_includes({"state": "stable"})

    cols, rows = matrix.pivot()
    assert cols == ("ubuntu-slim", "macos-26")
    assert rows == (
        ("3.10", "stable", "stable"),
        ("3.14", "stable", "stable"),
    )


def test_pivot_excluded_cell_is_missing():
    """An excluded combination renders the missing placeholder."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26"])
    matrix.add_variation("python-version", ["3.10", "3.14"])
    matrix.add_includes({"state": "stable"})
    matrix.add_excludes({"os": "macos-26", "python-version": "3.14"})

    cols, rows = matrix.pivot()
    assert cols == ("ubuntu-slim", "macos-26")
    assert rows == (
        ("3.10", "stable", "stable"),
        ("3.14", "stable", "—"),
    )


def test_pivot_marks_unstable_row():
    """A partial state include flags the matching row's cells unstable."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26"])
    matrix.add_variation("python-version", ["3.14", "3.15"])
    matrix.add_includes({"state": "stable"})
    matrix.add_includes({"state": "unstable", "python-version": "3.15"})

    _, rows = matrix.pivot()
    assert rows == (
        ("3.14", "stable", "stable"),
        ("3.15", "unstable", "unstable"),
    )


def test_pivot_multi_job_cell_joins_states():
    """Several jobs at one intersection join their distinct states."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim"])
    matrix.add_variation("python-version", ["3.14"])
    matrix.add_variation("click-version", ["stable", "main"])
    matrix.add_includes({"state": "stable"})
    matrix.add_includes({"state": "unstable", "click-version": "main"})

    cols, rows = matrix.pivot()
    assert cols == ("ubuntu-slim",)
    assert rows == (("3.14", "stable, unstable"),)


def test_pivot_counts_tallies_the_jobs_behind_each_cell():
    """A cell collapsing several jobs counts them, per distinct state."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim"])
    matrix.add_variation("python-version", ["3.14"])
    matrix.add_variation("click-version", ["stable", "main", "released"])
    matrix.add_includes({"state": "stable"})
    matrix.add_includes({"state": "unstable", "click-version": "main"})

    assert matrix.pivot_counts() == {
        ("3.14", "ubuntu-slim"): Counter({"stable": 2, "unstable": 1})
    }


def test_pivot_counts_omits_an_empty_intersection():
    """An excluded combination is absent, not present with an empty counter."""
    matrix = Matrix()
    matrix.add_variation("os", ["ubuntu-slim", "macos-26"])
    matrix.add_variation("python-version", ["3.10", "3.14"])
    matrix.add_includes({"state": "stable"})
    matrix.add_excludes({"os": "macos-26", "python-version": "3.14"})

    tallies = matrix.pivot_counts()
    assert ("3.14", "macos-26") not in tallies
    assert set(tallies) == {
        ("3.10", "ubuntu-slim"),
        ("3.10", "macos-26"),
        ("3.14", "ubuntu-slim"),
    }
    assert all(tally == Counter({"stable": 1}) for tally in tallies.values())


def test_pivot_custom_axes_and_missing_marker():
    """pivot() accepts arbitrary axes, cell key and missing placeholder."""
    matrix = Matrix()
    matrix.add_variation("fruit", ["apple", "pear"])
    matrix.add_variation("city", ["paris", "tokyo"])
    matrix.add_includes({"taste": "sweet"})
    matrix.add_excludes({"fruit": "pear", "city": "tokyo"})

    cols, rows = matrix.pivot(
        row_axis="fruit", col_axis="city", cell_key="taste", missing="n/a"
    )
    assert cols == ("paris", "tokyo")
    assert rows == (
        ("apple", "sweet", "sweet"),
        ("pear", "sweet", "n/a"),
    )
