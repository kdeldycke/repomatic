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

"""Tests for the label matcher in :mod:`repomatic.labels`.

The rule schema is the one `actions/labeler` and `github/issue-labeler`
defined. These tests pin the semantics that survived the port in-tree, and the
one that deliberately did not: a label's content patterns are now OR-joined.
"""

from __future__ import annotations

import logging

import pytest
import yaml

from repomatic.bundle import get_data_content
from repomatic.config import Config, LabelsConfig
from repomatic.labels import (
    compile_content_pattern,
    load_content_rules,
    load_file_rules,
    match_content_rules,
    match_file_rules,
)


def _make_config(**labels_kwargs):
    return Config(labels=LabelsConfig(**labels_kwargs))


# -- Loading and merging file rules -------------------------------------------


def test_bundled_file_rules_load_without_a_config():
    """The bundled defaults stand alone: no project config needed to match."""
    rules = load_file_rules()
    assert rules
    assert all(isinstance(groups, list) for groups in rules.values())


def test_file_rules_single_matcher():
    """The simplest rule shape: label plus one glob list."""
    rules = load_file_rules(
        _make_config(
            file_rules=[
                {"label": "🥭 mango", "any-glob-to-any-file": ["orchard/mango*"]},
            ],
        )
    )
    assert rules["🥭 mango"] == [
        {"changed-files": [{"any-glob-to-any-file": ["orchard/mango*"]}]},
    ]


@pytest.mark.parametrize(
    "matcher",
    (
        "all-globs-to-all-files",
        "all-globs-to-any-file",
        "any-glob-to-all-files",
        "any-glob-to-any-file",
    ),
)
def test_file_rules_all_changed_files_matchers(matcher):
    """Every changed-files matcher normalizes into the same group shape."""
    rules = load_file_rules(
        _make_config(file_rules=[{"label": "🥭 mango", matcher: ["a", "b"]}])
    )
    assert rules["🥭 mango"] == [{"changed-files": [{matcher: ["a", "b"]}]}]


@pytest.mark.parametrize("matcher", ("base-branch", "head-branch"))
def test_file_rules_branch_matchers(matcher):
    """Branch matchers sit at the group's top level, not under changed-files."""
    rules = load_file_rules(
        _make_config(file_rules=[{"label": "🥭 mango", matcher: ["^release/"]}])
    )
    assert rules["🥭 mango"] == [{matcher: ["^release/"]}]


def test_file_rules_same_label_ored():
    """Two entries for one label become two groups, which match independently."""
    rules = load_file_rules(
        _make_config(
            file_rules=[
                {"label": "🥭 mango", "any-glob-to-any-file": ["orchard/*"]},
                {"label": "🥭 mango", "head-branch": ["^mango/"]},
            ],
        )
    )
    assert len(rules["🥭 mango"]) == 2
    assert match_file_rules(rules, ["orchard/tree.md"]) == {"🥭 mango"}
    assert match_file_rules(rules, ["other.md"], head_branch="mango/graft") == {
        "🥭 mango"
    }


def test_project_rules_extend_a_bundled_label():
    """Adding to a label upstream already declares keeps the upstream group."""
    rules = load_file_rules(
        _make_config(
            file_rules=[
                {"label": "📚 documentation", "any-glob-to-any-file": ["*.rst"]}
            ]
        )
    )
    assert match_file_rules(rules, ["docs/index.md"]) == {"📚 documentation"}
    assert match_file_rules(rules, ["guide.rst"]) == {"📚 documentation"}


@pytest.mark.parametrize(
    ("rule", "warning"),
    (
        pytest.param(
            {"any-glob-to-any-file": ["a"]},
            "Skipping file rule without `label`",
            id="unlabelled",
        ),
        pytest.param(
            {"label": "🥭 mango"},
            "Skipping file rule for label '🥭 mango' with no matchers",
            id="matcherless",
        ),
    ),
)
def test_file_rules_skip_unusable_entries(caplog, rule, warning):
    """An entry that cannot be attributed or cannot match is dropped, loudly.

    A matcherless group would otherwise AND nothing and label every pull
    request, which is the worst outcome for a precision-first labeller.
    """
    with caplog.at_level(logging.WARNING):
        rules = load_file_rules(_make_config(file_rules=[rule]))
    assert "🥭 mango" not in rules
    assert any(warning in record.message for record in caplog.records)


def test_file_rules_warn_on_unknown_keys(caplog):
    """A typo'd matcher name is reported rather than silently ignored."""
    with caplog.at_level(logging.WARNING):
        load_file_rules(
            _make_config(
                file_rules=[
                    {
                        "label": "🥭 mango",
                        "any-glob-to-any-file": ["a"],
                        "any-glob-to-any-fil": ["b"],
                    },
                ],
            )
        )
    assert any("any-glob-to-any-fil" in record.message for record in caplog.records)


@pytest.mark.parametrize("wrapper", ("all", "any"))
def test_file_rules_group_wrappers(wrapper):
    """`any` / `all` wrappers recurse into nested sub-groups."""
    rules = load_file_rules(
        _make_config(
            file_rules=[
                {
                    "label": "🥭 mango",
                    wrapper: [
                        {"any-glob-to-any-file": ["orchard/*"]},
                        {"head-branch": ["^mango/"]},
                    ],
                },
            ],
        )
    )
    assert rules["🥭 mango"] == [
        {
            wrapper: [
                {"changed-files": [{"any-glob-to-any-file": ["orchard/*"]}]},
                {"head-branch": ["^mango/"]},
            ],
        },
    ]


# -- Matching file rules ------------------------------------------------------

MATCHER_CASES = (
    # (matcher, globs, files, expected)
    pytest.param(
        "any-glob-to-any-file", ["*.md"], ["a.md", "b.py"], True, id="any-any-hit"
    ),
    pytest.param(
        "any-glob-to-any-file", ["*.md"], ["a.py", "b.py"], False, id="any-any-miss"
    ),
    pytest.param(
        "any-glob-to-all-files", ["*.md"], ["a.md", "b.md"], True, id="any-all-hit"
    ),
    pytest.param(
        "any-glob-to-all-files", ["*.md"], ["a.md", "b.py"], False, id="any-all-miss"
    ),
    pytest.param(
        "all-globs-to-any-file",
        ["docs/**", "**/*.md"],
        ["docs/a.md", "b.py"],
        True,
        id="all-any-hit",
    ),
    pytest.param(
        # Each glob is satisfied, but never both by the same file.
        "all-globs-to-any-file",
        ["docs/**", "**/*.md"],
        ["docs/a.py", "b.md"],
        False,
        id="all-any-miss",
    ),
    pytest.param(
        "all-globs-to-all-files",
        ["docs/**", "**/*.md"],
        ["docs/a.md", "docs/b.md"],
        True,
        id="all-all-hit",
    ),
    pytest.param(
        "all-globs-to-all-files",
        ["docs/**", "**/*.md"],
        ["docs/a.md", "other.md"],
        False,
        id="all-all-miss",
    ),
)


@pytest.mark.parametrize(("matcher", "globs", "files", "expected"), MATCHER_CASES)
def test_changed_files_quantifiers(matcher, globs, files, expected):
    """The four matcher names cross the any/all quantifiers over globs & files."""
    rules = {"🥭 mango": [{"changed-files": [{matcher: globs}]}]}
    assert bool(match_file_rules(rules, files)) is expected


@pytest.mark.parametrize("matcher", [case.values[0] for case in MATCHER_CASES])
def test_no_changed_files_never_matches(matcher):
    """An empty diff matches nothing, whichever quantifier the rule names.

    Three of the four matchers quantify universally over files, so an
    unguarded `all()` would read as vacuously true and label an empty pull
    request with every glob rule at once.
    """
    rules = {"🥭 mango": [{"changed-files": [{matcher: ["**"]}]}]}
    assert match_file_rules(rules, []) == set()


@pytest.mark.parametrize(
    ("glob_pattern", "path", "expected"),
    (
        # DOTGLOB: half the interesting paths start with a dot.
        pytest.param(".github/**/*", ".github/workflows/x.yaml", True, id="dotglob"),
        pytest.param(".github/**/*", ".github/funding.yml", True, id="globstar-empty"),
        pytest.param("**/pyproject.toml", "sub/pyproject.toml", True, id="globstar"),
        pytest.param("*.lock", "sub/uv.lock", False, id="star-stops-at-slash"),
        pytest.param("{a,b}/*.py", "b/x.py", True, id="brace"),
        # NEGATEALL: a lone exclusion means "everything else", not "nothing".
        pytest.param("!**/*.md", "cli.py", True, id="negate-keeps-other"),
        pytest.param("!**/*.md", "readme.md", False, id="negate-drops-match"),
    ),
)
def test_glob_dialect_follows_minimatch(glob_pattern, path, expected):
    """The globs keep the `minimatch` semantics the retired action applied."""
    rules = {
        "🥭 mango": [{"changed-files": [{"any-glob-to-any-file": [glob_pattern]}]}]
    }
    assert bool(match_file_rules(rules, [path])) is expected


def test_group_conditions_are_anded():
    """Every condition in one group must hold for the group to match."""
    rules = {
        "🥭 mango": [
            {
                "changed-files": [{"any-glob-to-any-file": ["orchard/*"]}],
                "head-branch": ["^mango/"],
            },
        ],
    }
    assert match_file_rules(rules, ["orchard/a.md"], head_branch="mango/x") == {
        "🥭 mango"
    }
    assert match_file_rules(rules, ["orchard/a.md"], head_branch="pear/x") == set()
    assert match_file_rules(rules, ["barn/a.md"], head_branch="mango/x") == set()


def test_empty_group_matches_nothing():
    """A group with no conditions is inert, never a catch-all."""
    assert match_file_rules({"🥭 mango": [{}]}, ["anything.md"]) == set()


@pytest.mark.parametrize(
    ("branch_key", "head", "base", "expected"),
    (
        pytest.param("head-branch", "mango/graft", "main", True, id="head-hit"),
        pytest.param("head-branch", "pear/graft", "main", False, id="head-miss"),
        pytest.param("base-branch", "x", "mango-release", True, id="base-hit"),
        pytest.param("base-branch", "x", "main", False, id="base-miss"),
        pytest.param("head-branch", "", "main", False, id="head-absent"),
    ),
)
def test_branch_matchers(branch_key, head, base, expected):
    """Branch patterns are unanchored regexes over the branch name."""
    rules = {"🥭 mango": [{branch_key: ["^mango"]}]}
    matched = match_file_rules(rules, ["a.md"], head_branch=head, base_branch=base)
    assert bool(matched) is expected


@pytest.mark.parametrize(
    ("wrapper", "files", "expected"),
    (
        pytest.param("any", ["orchard/a.md"], True, id="any-first"),
        pytest.param("any", ["barn/a.py"], True, id="any-second"),
        pytest.param("any", ["shed/a.txt"], False, id="any-neither"),
        pytest.param("all", ["orchard/a.md"], False, id="all-partial"),
    ),
)
def test_group_wrappers_match(wrapper, files, expected):
    """`any` OR's its sub-groups; `all` AND's them."""
    rules = {
        "🥭 mango": [
            {
                wrapper: [
                    {"changed-files": [{"any-glob-to-any-file": ["orchard/*"]}]},
                    {"changed-files": [{"any-glob-to-any-file": ["barn/*"]}]},
                ],
            },
        ],
    }
    assert bool(match_file_rules(rules, files)) is expected


# -- Loading and merging content rules ----------------------------------------


def test_content_rules_same_label_merged():
    """Repeating a label concatenates its patterns rather than replacing them."""
    rules = load_content_rules(
        _make_config(
            content_rules=[
                {"label": "🥭 mango", "patterns": ["mango"]},
                {"label": "🥭 mango", "patterns": ["papaya"]},
            ],
        )
    )
    assert rules["🥭 mango"] == ["mango", "papaya"]


@pytest.mark.parametrize(
    ("rule", "warning"),
    (
        pytest.param(
            {"patterns": ["mango"]},
            "Skipping content rule without `label`",
            id="unlabelled",
        ),
        pytest.param(
            {"label": "🥭 mango", "patterns": []},
            "Skipping content rule for label '🥭 mango' with no patterns",
            id="patternless",
        ),
    ),
)
def test_content_rules_skip_unusable_entries(caplog, rule, warning):
    """An entry with no label or no pattern is dropped, loudly."""
    with caplog.at_level(logging.WARNING):
        rules = load_content_rules(_make_config(content_rules=[rule]))
    assert "🥭 mango" not in rules
    assert any(warning in record.message for record in caplog.records)


def test_content_rules_warn_on_unknown_keys(caplog):
    """A typo'd key is reported; the rule's valid part still loads."""
    with caplog.at_level(logging.WARNING):
        rules = load_content_rules(
            _make_config(
                content_rules=[
                    {"label": "🥭 mango", "patterns": ["mango"], "pattern": ["papaya"]},
                ],
            )
        )
    assert rules["🥭 mango"] == ["mango"]
    assert any("'pattern'" in record.message for record in caplog.records)


# -- Matching content rules ---------------------------------------------------


def test_content_patterns_are_or_joined():
    """Any one pattern matching applies the label.

    This is the single semantic that deliberately diverges from
    `github/issue-labeler`, which AND-joined them and so made the obvious
    "any of these keywords" rule silently dead.
    """
    rules = {"🥭 mango": ["mango", "papaya", "guava"]}
    assert match_content_rules(rules, "a crate of papaya") == {"🥭 mango"}
    assert match_content_rules(rules, "mango, papaya and guava") == {"🥭 mango"}
    assert match_content_rules(rules, "a crate of pears") == set()


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    (
        pytest.param("/mango/i", "MANGO crate", True, id="slashed-ignorecase"),
        pytest.param("mango", "MANGO crate", False, id="bare-is-case-sensitive"),
        pytest.param("mango", "mango crate", True, id="bare-matches"),
        pytest.param(r"/\bripe\b/i", "unripe fruit", False, id="word-boundary"),
        pytest.param(r"/\bripe\b/i", "a Ripe mango", True, id="word-boundary-hit"),
        pytest.param("/^mango$/m", "pears\nmango\n", True, id="multiline"),
        pytest.param("/mango.crate/s", "mango\ncrate", True, id="dotall"),
        # A JavaScript-only flag is tolerated: it cannot change the verdict.
        pytest.param("/mango/gu", "a mango", True, id="js-only-flags-ignored"),
    ),
)
def test_content_pattern_forms(pattern, text, expected):
    """Both the bare and `/body/flags` spellings behave as documented."""
    assert bool(match_content_rules({"🥭 mango": [pattern]}, text)) is expected


def test_malformed_pattern_is_skipped_not_fatal(caplog):
    """One bad regex must not take the whole labelling run with it."""
    with caplog.at_level(logging.WARNING):
        matched = match_content_rules(
            {"🥭 mango": ["/[unclosed/"], "🍐 pear": ["pear"]},
            "a pear and a mango",
        )
    assert matched == {"🍐 pear"}
    assert compile_content_pattern("/[unclosed/") is None
    assert any("malformed content pattern" in r.message for r in caplog.records)


# -- Bundled defaults ---------------------------------------------------------


def test_bundled_content_patterns_are_case_insensitive():
    """Users capitalize freely, so every shipped pattern needs the `i` flag.

    A bare pattern is matched case-sensitively, so `/…/i` is what makes `Bug`
    and `README` reachable. These defaults ship to every downstream repo, where
    a case-sensitive rule is a near-dead label everywhere at once.
    """
    parsed = yaml.safe_load(get_data_content("labeller-content-based.yaml"))
    assert parsed, "bundled content rules must not be empty"
    for label, patterns in parsed.items():
        for pattern in patterns:
            assert pattern.endswith("/i"), (
                f"bundled pattern {pattern!r} on {label!r} is case-sensitive"
            )


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        pytest.param("Traceback on an empty crate", {"🐛 bug"}, id="bug"),
        pytest.param("Update the README", {"📚 documentation"}, id="docs"),
        pytest.param("A workflow needs coverage", {"🤖 ci"}, id="ci"),
        pytest.param("Bump the changelog", {"🆙 changelog"}, id="changelog"),
        # `fix` must not fire inside `prefix`: every bundled pattern is
        # word-anchored precisely to stop this.
        pytest.param("prefix handling is off", set(), id="no-substring-match"),
        pytest.param("Nothing relevant at all", set(), id="no-match"),
    ),
)
def test_bundled_content_rules_match_as_advertised(text, expected):
    """The shipped keyword lists fire on their keyword and nothing else."""
    assert match_content_rules(load_content_rules(), text) == expected


@pytest.mark.parametrize(
    ("files", "expected"),
    (
        pytest.param(["changelog.md"], {"🆙 changelog"}, id="changelog"),
        pytest.param(["uv.lock"], {"🔗 dependencies"}, id="lockfile"),
        pytest.param(["docs/guide/index.md"], {"📚 documentation"}, id="docs-tree"),
        pytest.param([".github/funding.yml"], {"🤖 ci", "💖 sponsor"}, id="funding"),
        pytest.param(["repomatic/cli.py"], set(), id="source-is-unlabelled"),
    ),
)
def test_bundled_file_rules_match_as_advertised(files, expected):
    """The shipped globs cover the paths they name, and no others."""
    assert match_file_rules(load_file_rules(), files) == expected
