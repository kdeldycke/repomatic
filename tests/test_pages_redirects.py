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

"""Tests for the Cloudflare Pages ``_redirects`` engine replica.

Every behaviour asserted here was transcribed from the reference
implementation in `cloudflare/workers-sdk`, not from the documentation: the
budget accounting, the trailing-slash literalism, the order in which exact
rules and patterns are probed, and the rules dropped at run time are precisely
the parts the documentation does not state.
"""

from __future__ import annotations

import pytest

from repomatic.pages_redirects import (
    MAX_DYNAMIC_RULES,
    apply_rule,
    discarded_rules,
    evaluate,
    misordered_statics,
    parse_redirects,
    rule_pattern,
    sample_path,
    shadowed_statics,
)


def test_statics_first_ride_the_static_budget():
    """Exact rules ahead of the first dynamic one never touch the 100 budget."""
    text = "\n".join(
        [f"/old-{index} /new-{index} 301" for index in range(200)]
        + ["/blog/* /articles/:splat 301"]
    )
    parsed = parse_redirects(text)
    assert len(parsed.rules) == 201
    assert not parsed.invalid
    assert parsed.aborted_at_line is None
    assert not misordered_statics(parsed.rules)


def test_statics_after_a_dynamic_burn_the_dynamic_budget_until_the_file_dies():
    """The undocumented kill switch: rule 101 of the mixed stream aborts the file.

    Not "is skipped": the parser breaks, so every remaining line is discarded
    and nothing in the deploy pipeline says so.
    """
    lines = ["/blog/* /articles/:splat 301"]
    lines += [f"/old-{index} /new-{index} 301" for index in range(150)]
    parsed = parse_redirects("\n".join(lines))
    # One dynamic plus 99 charged statics fit the budget of 100; the 101st
    # charged rule sits on line 101 and kills the rest of the file.
    assert parsed.aborted_at_line == 101
    assert len(parsed.rules) == 100
    assert len(misordered_statics(parsed.rules)) == 99


def test_misordered_statics_names_every_late_exact_rule():
    """Each late exact rule is one slot of headroom lost, so each is reported."""
    parsed = parse_redirects(
        "/a /b 301\n/blog/* /articles/:splat 301\n/c /d 301\n/e /f 301\n"
    )
    late = misordered_statics(parsed.rules)
    assert [rule.source for rule in late] == ["/c", "/e"]


def test_duplicate_sources_are_dropped_first_wins():
    parsed = parse_redirects("/a /b 301\n/a /c 301\n")
    assert [rule.destination for rule in parsed.rules] == ["/b"]
    assert "duplicate rule" in parsed.invalid[0].message


def test_inline_comments_and_default_status():
    parsed = parse_redirects("/a /b  # why this rule exists\n")
    assert parsed.rules[0].status == 302
    assert parsed.rules[0].destination == "/b"


@pytest.mark.parametrize(
    ("line", "fragment"),
    (
        pytest.param("/d /e 418", "Valid status codes", id="teapot-status"),
        pytest.param("/x/* /x/index.html", "Infinite loop", id="splat-into-index"),
        pytest.param("/x/ /x/index", "Infinite loop", id="trailing-slash-into-index"),
        pytest.param(
            "https://example.com/a /b 301",
            "Only relative URLs",
            id="absolute-source",
        ),
        pytest.param(
            "/a /b /c 301", "2 or 3 whitespace-separated", id="token-overflow"
        ),
        pytest.param(
            "/a https://example.com/b 200",
            "Proxy (200) redirects",
            id="proxy-to-absolute",
        ),
    ),
)
def test_engine_refusals(line, fragment):
    """Each refused shape is dropped with the engine's own message."""
    parsed = parse_redirects(line + "\n")
    assert not parsed.rules
    assert fragment in parsed.invalid[0].message


def test_overlong_lines_are_ignored():
    parsed = parse_redirects(f"/a{'a' * 2100} /b 301\n/ok /fine 301\n")
    assert [rule.source for rule in parsed.rules] == ["/ok"]
    assert "maximum allowed length" in parsed.invalid[0].message


def test_trailing_slashes_are_different_sources():
    """`/a` and `/a/` never match each other; only the splat bridges them."""
    parsed = parse_redirects("/a /b 301\n")
    rule = parsed.rules[0]
    assert apply_rule(rule, "/a") == "/b"
    assert evaluate(parsed.rules, "/a/") is None


def test_splat_matches_the_bare_trailing_slash_through_an_empty_capture():
    """`/dir/*` answers `/dir/` itself, the historically loaded WordPress case."""
    parsed = parse_redirects("/blog/* /articles/:splat 301\n")
    match = evaluate(parsed.rules, "/blog/")
    assert match is not None
    assert match[1] == "/articles/"


def test_placeholder_never_matches_a_slash_or_nothing():
    parsed = parse_redirects("/y/:m /y 301\n")
    assert evaluate(parsed.rules, "/y/2010/post") is None
    assert evaluate(parsed.rules, "/y/") is None
    assert evaluate(parsed.rules, "/y/2010") is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        pytest.param(
            "/fruits/pear /pear 301\n/fruits/* /basket/:splat 301\n",
            "/pear",
            id="exact-above-the-pattern",
        ),
        pytest.param(
            "/fruits/* /basket/:splat 301\n/fruits/pear /pear 301\n",
            "/basket/pear",
            id="exact-below-the-pattern",
        ),
    ),
)
def test_an_exact_rule_wins_only_above_the_first_dynamic_rule(text, expected):
    """The server's map of exact sources holds the rules above the first pattern.

    An exact rule below that point is probed in file order with the patterns,
    so a pattern above it that matches the same path answers first. The
    reference gives these two answers for these two files.
    """
    parsed = parse_redirects(text)
    match = evaluate(parsed.rules, "/fruits/pear")
    assert match is not None
    assert match[1] == expected


def test_shadowed_statics_pairs_each_dead_rule_with_the_pattern_that_answers():
    """A late exact rule that a pattern above it matches never fires."""
    parsed = parse_redirects(
        "/herbs/mint /mint 301\n"
        "/fruits/* /basket/:splat 301\n"
        "/fruits/pear /pear 301\n"  # Dead: the splat above answers first.
        "/roots/beet /beet 301\n"  # Late, but no pattern matches it.
    )
    assert [
        (rule.source, winner.source) for rule, winner in shadowed_statics(parsed.rules)
    ] == [("/fruits/pear", "/fruits/*")]
    # A late exact rule that nothing shadows still answers for itself.
    late = evaluate(parsed.rules, "/roots/beet")
    assert late is not None
    assert late[1] == "/beet"


@pytest.mark.parametrize(
    ("source", "request_path"),
    (
        pytest.param("/fruits/*/crate/*", "/fruits/pear/crate/3", id="two-splats"),
        pytest.param(
            "/fruits/:splat/*", "/fruits/pear/3", id="splat-beside-splat-name"
        ),
        pytest.param("/:kind/:kind", "/pear/pear", id="placeholder-name-twice"),
    ),
)
def test_a_source_that_cannot_compile_is_dropped_at_run_time(source, request_path):
    """The parser keeps the rule, then the server fails to build its pattern.

    The reference catches that error and drops the rule alone, so the rules
    around it keep working and nothing is reported.
    """
    parsed = parse_redirects(f"{source} /basket 301\n/herbs/* /garden 301\n")
    broken, working = parsed.rules
    assert not parsed.invalid
    assert not broken.compiles
    assert working.compiles
    assert apply_rule(broken, request_path) is None
    assert evaluate(parsed.rules, request_path) is None
    landing = evaluate(parsed.rules, "/herbs/mint")
    assert landing is not None
    assert landing[1] == "/garden"


@pytest.mark.parametrize(
    ("line", "expected"),
    (
        pytest.param("/tree/:kind /orchard/:kind 301", (), id="captured-name"),
        pytest.param("/tree/* /orchard/:splat 301", (), id="captured-splat"),
        pytest.param(
            "/tree/:kind /orchard/:variety 301", (":variety",), id="name-never-captured"
        ),
        pytest.param(
            "/tree/:kind /orchard/:splat 301",
            (":splat",),
            id="splat-name-without-splat",
        ),
        pytest.param("/tree/* /orchard/* 301", ("*",), id="literal-star"),
        # The server replaces `:kind` inside `:kindness` too, so nothing stays.
        pytest.param("/tree/:kind /orchard/:kindness 301", (), id="name-as-a-prefix"),
        # An exact source captures nothing: its destination is literal text.
        pytest.param(
            "/guide https://example.org/wiki/Help:Contents 301", (), id="exact-source"
        ),
    ),
)
def test_unresolved_names_what_stays_literal_in_a_destination(line, expected):
    """The engine reports nothing for these: the visitor gets `:name` in the URL."""
    (rule,) = parse_redirects(line + "\n").rules
    assert rule.unresolved == expected


def test_budget_constant_matches_the_reference():
    """The number the whole accounting hangs on, pinned against typos."""
    assert MAX_DYNAMIC_RULES == 100


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # An exact source is already the URL that stops working.
        ("/handbook", "/handbook"),
        ("/fruit/papaya/", "/fruit/papaya/"),
        # A placeholder stands in for itself, a splat for one segment.
        ("/fruit/:variety", "/fruit/variety"),
        ("/harvest/*", "/harvest/sample"),
        ("/:season/fruit/:variety", "/season/fruit/variety"),
    ],
)
def test_sample_path_concretizes_a_source(source: str, expected: str):
    """Every sampled path must match the rule it was derived from."""
    assert sample_path(source) == expected
    assert rule_pattern(source).match(sample_path(source))


def test_discarded_rules_recovers_the_abandoned_tail():
    """What the engine never read, named with its real line numbers.

    `parse_redirects` reports only that it stopped, because that is all
    production does. Recovering the tail is what lets the lint say which
    URLs went dead rather than how many rules did.
    """
    lines = [f"/p{index}/:x /new{index} 301" for index in range(101)]
    lines += ["/handbook /guide 301", "/faq /help 301"]
    text = "\n".join(lines)

    parsed = parse_redirects(text)
    assert parsed.aborted_at_line == 101

    abandoned = discarded_rules(text, parsed)
    assert [rule.source for rule in abandoned] == ["/p100/:x", "/handbook", "/faq"]
    # Line numbers are shifted back onto the real file, not the parsed tail.
    assert [rule.line_number for rule in abandoned] == [101, 102, 103]


def test_discarded_rules_is_empty_for_a_file_read_to_the_end():
    """No abort, nothing abandoned: the common case costs no second parse."""
    text = "/handbook /guide 301\n/fruit/* /harvest/:splat 301\n"
    parsed = parse_redirects(text)
    assert parsed.aborted_at_line is None
    assert discarded_rules(text, parsed) == []


def test_an_abandoned_source_can_be_captured_by_a_surviving_pattern():
    """The quiet failure: a lost exact rule whose URL now goes elsewhere.

    Losing a redirect is visible to anyone who follows the URL. Having it
    answered by a broader pattern that outlived it is not, since the request
    still redirects, just never where its author wrote.
    """
    lines = [f"/p{index}/:x /new{index} 301" for index in range(99)]
    lines += ["/fruit/* /harvest 301"]  # 100th dynamic rule, still parsed.
    lines += ["/burst/:y /boom 301"]  # 101st: the budget dies here.
    lines += ["/fruit/papaya /papaya-harvest 301"]  # Abandoned below the line.
    text = "\n".join(lines)

    parsed = parse_redirects(text)
    abandoned = discarded_rules(text, parsed)
    assert "/fruit/papaya" in [rule.source for rule in abandoned]

    landing = evaluate(parsed.rules, "/fruit/papaya")
    assert landing is not None
    assert landing[1] == "/harvest"
