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

import logging
from importlib.resources import files

import pytest
import yaml

from repomatic.config import Config, LabelsConfig
from repomatic.labels import (
    augment_labeller_content,
    serialize_content_rules,
    serialize_file_rules,
)

# -- File rules ---------------------------------------------------------------


def test_serialize_file_rules_empty():
    """Empty input yields empty string so the caller can skip the append."""
    assert serialize_file_rules([]) == ""


def test_serialize_file_rules_single_matcher():
    """The simplest rule shape: label plus one glob list."""
    output = serialize_file_rules([
        {"label": "📦 manager: apk", "any-glob-to-any-file": ["managers/apk*"]},
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {
        "📦 manager: apk": [
            {"changed-files": [{"any-glob-to-any-file": ["managers/apk*"]}]},
        ],
    }


@pytest.mark.parametrize(
    "matcher",
    [
        "any-glob-to-any-file",
        "any-glob-to-all-files",
        "all-globs-to-any-file",
        "all-globs-to-all-files",
    ],
)
def test_serialize_file_rules_all_changed_files_matchers(matcher):
    """Every actions/labeler changed-files matcher round-trips correctly."""
    output = serialize_file_rules([{"label": "x", matcher: ["a", "b"]}])
    parsed = yaml.safe_load(output)
    assert parsed == {"x": [{"changed-files": [{matcher: ["a", "b"]}]}]}


@pytest.mark.parametrize("matcher", ["head-branch", "base-branch"])
def test_serialize_file_rules_branch_matchers(matcher):
    """Branch matchers sit at the group's top level, not inside changed-files."""
    output = serialize_file_rules([{"label": "x", matcher: ["^foo/"]}])
    parsed = yaml.safe_load(output)
    assert parsed == {"x": [{matcher: ["^foo/"]}]}


def test_serialize_file_rules_combined_conditions_in_one_group():
    """Multiple matchers in one entry AND together as a single group."""
    output = serialize_file_rules([
        {
            "label": "x",
            "any-glob-to-any-file": ["a"],
            "all-globs-to-all-files": ["b"],
            "head-branch": ["^feat/"],
        },
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {
        "x": [
            {
                "changed-files": [
                    {"any-glob-to-any-file": ["a"]},
                    {"all-globs-to-all-files": ["b"]},
                ],
                "head-branch": ["^feat/"],
            },
        ],
    }


def test_serialize_file_rules_same_label_ored():
    """Repeating a label across entries produces multiple OR'd groups."""
    output = serialize_file_rules([
        {"label": "x", "any-glob-to-any-file": ["a"]},
        {"label": "x", "head-branch": ["^foo/"]},
        {"label": "y", "any-glob-to-any-file": ["b"]},
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {
        "x": [
            {"changed-files": [{"any-glob-to-any-file": ["a"]}]},
            {"head-branch": ["^foo/"]},
        ],
        "y": [{"changed-files": [{"any-glob-to-any-file": ["b"]}]}],
    }


def test_serialize_file_rules_skips_unlabeled(caplog):
    """Entries without a label are skipped with a warning."""
    caplog.set_level(logging.WARNING)
    output = serialize_file_rules([
        {"any-glob-to-any-file": ["a"]},
        {"label": "x", "any-glob-to-any-file": ["b"]},
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {"x": [{"changed-files": [{"any-glob-to-any-file": ["b"]}]}]}
    assert any(
        "Skipping file rule without `label`" in r.message for r in caplog.records
    )


def test_serialize_file_rules_skips_matcherless(caplog):
    """A label with no matchers would label every PR; skip it loudly."""
    caplog.set_level(logging.WARNING)
    assert serialize_file_rules([{"label": "x"}]) == ""
    assert any(
        "Skipping file rule for label 'x' with no matchers" in r.message
        for r in caplog.records
    )


def test_serialize_file_rules_warns_on_unknown_keys(caplog):
    """Unknown keys are dropped with a warning, leaving valid matchers intact."""
    caplog.set_level(logging.WARNING)
    output = serialize_file_rules([
        {"label": "x", "any-glob-to-any-file": ["a"], "made-up": ["nope"]},
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {"x": [{"changed-files": [{"any-glob-to-any-file": ["a"]}]}]}
    assert any(
        "Unknown file rule key 'made-up' in label 'x'" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("wrapper", ["any", "all"])
def test_serialize_file_rules_group_wrappers(wrapper):
    """`any`/`all` wrappers render as nested groups in the labeller YAML."""
    output = serialize_file_rules([
        {
            "label": "x",
            wrapper: [
                {"any-glob-to-any-file": ["a"]},
                {"head-branch": ["^foo/"]},
            ],
        },
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {
        "x": [
            {
                wrapper: [
                    {"changed-files": [{"any-glob-to-any-file": ["a"]}]},
                    {"head-branch": ["^foo/"]},
                ],
            },
        ],
    }


def test_serialize_file_rules_nested_group_wrappers():
    """Group wrappers nest recursively, covering the full labeller v5+ schema."""
    output = serialize_file_rules([
        {
            "label": "x",
            "any": [
                {
                    "all": [
                        {"any-glob-to-any-file": ["a"]},
                        {"head-branch": ["^foo/"]},
                    ],
                },
                {"any-glob-to-any-file": ["b"]},
            ],
        },
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {
        "x": [
            {
                "any": [
                    {
                        "all": [
                            {"changed-files": [{"any-glob-to-any-file": ["a"]}]},
                            {"head-branch": ["^foo/"]},
                        ],
                    },
                    {"changed-files": [{"any-glob-to-any-file": ["b"]}]},
                ],
            },
        ],
    }


def test_serialize_file_rules_group_wrapper_alongside_siblings():
    """A group may carry both flat matchers and a wrapper; both survive."""
    output = serialize_file_rules([
        {
            "label": "x",
            "any-glob-to-any-file": ["a"],
            "any": [{"head-branch": ["^foo/"]}],
        },
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {
        "x": [
            {
                "changed-files": [{"any-glob-to-any-file": ["a"]}],
                "any": [{"head-branch": ["^foo/"]}],
            },
        ],
    }


def test_serialize_file_rules_skips_empty_sub_group(caplog):
    """Empty sub-groups inside `any`/`all` are dropped with a warning."""
    caplog.set_level(logging.WARNING)
    output = serialize_file_rules([
        {
            "label": "x",
            "any": [
                {},  # empty: dropped.
                {"any-glob-to-any-file": ["a"]},
            ],
        },
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {
        "x": [
            {
                "any": [
                    {"changed-files": [{"any-glob-to-any-file": ["a"]}]},
                ],
            },
        ],
    }
    assert any(
        "Skipping empty 'any' sub-group for label 'x'" in r.message
        for r in caplog.records
    )


# -- Content rules ------------------------------------------------------------


def test_serialize_content_rules_empty():
    assert serialize_content_rules([]) == ""


def test_serialize_content_rules_basic():
    """Label plus patterns round-trip to the issue-labeler shape."""
    output = serialize_content_rules([
        {"label": "🔌 bar-plugin", "patterns": [r"/\bxbar\b|\bswiftbar\b/i"]},
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {"🔌 bar-plugin": [r"/\bxbar\b|\bswiftbar\b/i"]}


def test_serialize_content_rules_same_label_merged():
    """Repeating a label concatenates its patterns, preserving order."""
    output = serialize_content_rules([
        {"label": "x", "patterns": ["a", "b"]},
        {"label": "x", "patterns": ["c"]},
        {"label": "y", "patterns": ["d"]},
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {"x": ["a", "b", "c"], "y": ["d"]}


@pytest.mark.parametrize(
    ("rules", "expected_count"),
    (
        # A single entry listing several keywords.
        (({"label": "x", "patterns": ["alpha", "beta"]},), 2),
        # The sneakier shape: each entry is fine alone, only the
        # concatenation of a repeated label crosses into AND.
        (
            (
                {"label": "x", "patterns": ["alpha"]},
                {"label": "x", "patterns": ["beta"]},
            ),
            2,
        ),
    ),
    ids=("single-entry", "repeated-label"),
)
def test_serialize_content_rules_warns_on_and_join(caplog, rules, expected_count):
    """A label left with several patterns can never fire, so it is warned about.

    `github/issue-labeler` requires every pattern to match the same issue, so a
    keyword list reads as "all of these" rather than "any of these".
    """
    caplog.set_level(logging.WARNING)
    output = serialize_content_rules(list(rules))
    # The patterns are still emitted: AND is a real capability, and the
    # serializer cannot tell a deliberate conjunction from the mistake.
    assert yaml.safe_load(output) == {"x": ["alpha", "beta"]}
    assert any(
        f"lists {expected_count} patterns" in r.message
        and "never be applied" in r.message
        for r in caplog.records
    )


def test_serialize_content_rules_single_pattern_is_silent(caplog):
    """The correct shape, one alternation per label, warns about nothing."""
    caplog.set_level(logging.WARNING)
    serialize_content_rules([{"label": "x", "patterns": [r"/\ba\b|\bb\b/i"]}])
    assert not [r for r in caplog.records if "never be applied" in r.message]


def test_serialize_content_rules_skips_empty_patterns(caplog):
    caplog.set_level(logging.WARNING)
    output = serialize_content_rules([
        {"label": "x", "patterns": []},
        {"label": "y", "patterns": ["yes"]},
    ])
    parsed = yaml.safe_load(output)
    assert parsed == {"y": ["yes"]}
    assert any(
        "Skipping content rule for label 'x' with no patterns" in r.message
        for r in caplog.records
    )


# -- Augment helper -----------------------------------------------------------


def _make_config(**labels_kwargs):
    return Config(labels=LabelsConfig(**labels_kwargs))


def test_augment_passes_through_unknown_source():
    """Non-labeller source files come back unchanged."""
    config = _make_config(file_rules=[{"label": "x", "any-glob-to-any-file": ["a"]}])
    assert augment_labeller_content("labels.toml", "x\n", config) == "x\n"


def test_augment_returns_bundled_when_no_rules():
    """No structured rules: bundled content untouched."""
    config = _make_config()
    bundled = "bundled: yes\n"
    assert (
        augment_labeller_content("labeller-file-based.yaml", bundled, config) == bundled
    )


def test_augment_appends_file_rules():
    """Bundled content keeps its labels and gains the structured ones."""
    config = _make_config(
        file_rules=[{"label": "x", "any-glob-to-any-file": ["a"]}],
    )
    result = augment_labeller_content(
        "labeller-file-based.yaml",
        "bundled: yes\n",
        config,
    )
    parsed = yaml.safe_load(result)
    assert set(parsed) == {"bundled", "x"}
    assert parsed["x"] == [{"changed-files": [{"any-glob-to-any-file": ["a"]}]}]


def test_augment_appends_content_rules():
    """Same merge shape works for the content-based labeller."""
    config = _make_config(
        content_rules=[{"label": "x", "patterns": ["xbar"]}],
    )
    result = augment_labeller_content(
        "labeller-content-based.yaml",
        "bundled:\n  - upstream\n",
        config,
    )
    parsed = yaml.safe_load(result)
    assert parsed == {"bundled": ["upstream"], "x": ["xbar"]}


def test_augment_none_config():
    """A None config (caller opts out) preserves the bundled content."""
    bundled = "bundled: yes\n"
    assert (
        augment_labeller_content("labeller-file-based.yaml", bundled, None) == bundled
    )


def test_bundled_content_rules_are_single_alternations():
    """Every bundled content rule must carry exactly one pattern.

    These defaults ship to every downstream repository, so an AND-joined list
    here is a dead label everywhere at once: `github/issue-labeler` requires
    all of a label's patterns to match the same issue. All six bundled rules
    were once keyword lists and fired for nobody.
    """
    bundled = (
        files("repomatic.data")
        .joinpath("labeller-content-based.yaml")
        .read_text(encoding="UTF-8")
    )
    parsed = yaml.safe_load(bundled)
    assert parsed, "bundled content rules must not be empty"
    multi = {label: patterns for label, patterns in parsed.items() if len(patterns) > 1}
    assert not multi, (
        f"AND-joined bundled rules can never fire: {multi}. "
        r"Collapse each into one alternation, like /\bfoo\b|\bbar\b/i."
    )
    # A bare pattern is matched case-sensitively, so the `/…/i` form is what
    # makes `Bug` and `README` reachable.
    assert all(patterns[0].endswith("/i") for patterns in parsed.values()), (
        "every bundled pattern should be a case-insensitive /…/i regex"
    )
