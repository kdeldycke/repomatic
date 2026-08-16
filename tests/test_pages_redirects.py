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
budget accounting, the trailing-slash literalism and the exact-before-pattern
probing are precisely the parts the documentation does not state.
"""

from __future__ import annotations

import pytest

from repomatic.pages_redirects import (
    MAX_DYNAMIC_RULES,
    apply_rule,
    evaluate,
    misordered_statics,
    parse_redirects,
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


def test_exact_rules_probe_before_patterns_wherever_they_sit():
    """The asset server hashes exact sources and probes them first.

    This is what makes the statics-first reorder the lint recommends
    behaviour-preserving: a file where a pattern shadows a later exact rule
    already resolved the exact one at runtime.
    """
    parsed = parse_redirects("/a/* /pattern/:splat 301\n/a/b /exact 301\n")
    match = evaluate(parsed.rules, "/a/b")
    assert match is not None
    assert match[1] == "/exact"


def test_budget_constant_matches_the_reference():
    """The number the whole accounting hangs on, pinned against typos."""
    assert MAX_DYNAMIC_RULES == 100
