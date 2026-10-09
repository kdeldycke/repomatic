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

"""A faithful Python replica of the engine Cloudflare Pages runs ``_redirects`` on.

Cloudflare's documentation describes the file format; it does not describe the
accounting, and the accounting is where rules die. A site once lost the last 18
rules of its file for years this way, silently: `wrangler pages deploy` prints
nothing when the parser discards lines, and a dead redirect looks exactly like
a URL nobody visits. This module replicates the reference implementation so
`lint-repo` can audit a committed file the way production will read it, before
production reads it.

Transcribed on 2026-08-10 from the engine itself, not from the documentation,
and compared again on 2026-10-08 with
[cloudflare/workers-sdk@6478d32](https://github.com/cloudflare/workers-sdk/commit/6478d32f66da0e95d9de79aa572bcacb23bd9a4b),
run on the same inputs:

- Parsing: ``packages/workers-shared/utils/configuration/parseRedirects.ts`` in
  [cloudflare/workers-sdk](https://github.com/cloudflare/workers-sdk), as
  bundled in wrangler 4.118 (the same code path Miniflare uses, and the same
  parser family the Pages asset server feeds on).
- Sorting into the two rule sets: ``constructRedirects`` in
  ``packages/workers-shared/utils/configuration/constructConfiguration.ts``.
- Matching: ``packages/workers-shared/asset-worker/src/utils/rules-engine.ts``,
  called by ``packages/pages-shared/asset-server/handler.ts``.

The five rules of the engine that the documentation does not state:

1. **A static rule is only free while it appears before the first dynamic
   rule.** The parser flips ``canCreateStaticRule`` to false permanently at the
   first source containing ``*`` or ``:placeholder``. Every later rule, however
   static it looks, is charged against the dynamic budget.
2. **The dynamic budget is 100, and blowing it aborts the file.** Rule 101 of
   that mixed stream does not get skipped: the parser breaks out of the loop,
   discarding every remaining line. Order is therefore not a style choice, it
   decides which rules exist.
3. **Matching is anchored and literal about trailing slashes.** A placeholder
   compiles to ``[^/]+`` (at least one character, never a slash, never empty),
   a splat to ``.*`` (may be empty), and the whole source to ``^...$``.
   ``/a/:b`` does not match ``/x/y/`` and ``/a/*`` matches ``/a/`` with an
   empty splat.
4. **An exact rule is only probed first while it is free.** The exact rules
   above the first dynamic one go to a map that the server reads before
   anything else. An exact rule below that point joins the dynamic rules, in
   file order: a pattern above it that matches the same path answers first,
   and the exact rule never fires.
5. **A source that cannot compile is dropped at run time.** A second ``*``, a
   ``*`` beside ``:splat``, or a placeholder name used twice gives a regular
   expression with a duplicate group. The parser keeps such a rule, and the
   server discards it when the regular expression fails to build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from functools import cache
from urllib.parse import urlsplit

MAX_LINE_LENGTH = 2000
MAX_STATIC_RULES = 2000
MAX_DYNAMIC_RULES = 100
PERMITTED_STATUS_CODES = frozenset({200, 301, 302, 303, 307, 308})

SPLAT_REGEX = re.compile(r"\*")
PLACEHOLDER_REGEX = re.compile(r":[A-Za-z]\w*")

# What a source captures, in the order the server replaces it in a destination.
SOURCE_TOKEN_REGEX = re.compile(r"\*|:[A-Za-z]\w*")

URL_REGEX = re.compile(r"^https://+(?P<host>[^/]+)/?(?P<path>.*)")
HOST_WITH_PORT_REGEX = re.compile(r".*:\d+$")

# The engine strips inline comments: whitespace followed by `#` ends the rule.
INLINE_COMMENT_REGEX = re.compile(r"\s+#.*$")

# A destination the engine's own .html / /index stripping would re-trigger.
# The unescaped dot is the reference's, verbatim: fidelity to rules-engine.ts
# beats regex hygiene here, so it also matches an `/indexXhtml` the way the
# engine does.
INDEX_DESTINATION_REGEX = re.compile(r"/index(.html)?$")

# Everything the reference escapes before turning a source into a regex.
ESCAPE_REGEX_CHARACTERS = re.compile(r"[-/\\^$*+?.()|[\]{}]")


@dataclass(frozen=True)
class Rule:
    source: str
    destination: str
    status: int
    line_number: int

    @property
    def is_dynamic(self) -> bool:
        return bool(SPLAT_REGEX.search(self.source)) or bool(
            PLACEHOLDER_REGEX.search(self.source)
        )

    @property
    def compiles(self) -> bool:
        """Whether the server can build the regular expression of the source.

        It cannot when two groups take the same name: a second `*`, a `*`
        beside `:splat`, or a placeholder name used twice. The parser keeps
        such a rule, and the server then drops it without a word.
        """
        try:
            rule_pattern(self.source)
        except re.error:
            return False
        return True

    @property
    def unresolved(self) -> tuple[str, ...]:
        """Destination tokens that no capture of the source replaces.

        The server replaces `:name` in the destination for each group the
        source captured, and `:splat` for a `*`. Any other `:name` stays in the
        URL as literal text, and so does a `*`: the engine reports nothing, and
        the visitor lands on an address that holds a colon and a word.

        An exact source captures nothing, so its destination is literal text
        from end to end and this answers an empty tuple for it: a colon in the
        address of another site is ordinary there.
        """
        if not self.is_dynamic:
            return ()
        leftover = self.destination
        for token in SOURCE_TOKEN_REGEX.findall(self.source):
            leftover = leftover.replace(":splat" if token == "*" else token, "")
        tokens = dict.fromkeys(PLACEHOLDER_REGEX.findall(leftover))
        if "*" in self.source and "*" in self.destination:
            tokens["*"] = None
        return tuple(tokens)


@dataclass(frozen=True)
class Invalid:
    message: str
    line: str | None = None
    line_number: int | None = None


@dataclass
class ParseResult:
    rules: list[Rule] = field(default_factory=list)
    invalid: list[Invalid] = field(default_factory=list)
    aborted_at_line: int | None = None
    """Line number from which the parser discarded the rest of the file, if it did."""


def _extract_pathname(path: str, include_search: bool, include_hash: bool) -> str:
    """Pragmatic stand-in for the reference's WHATWG ``new URL()`` normalization.

    The reference resolves the token against a dummy base and keeps the
    pathname. For the plain ASCII, single-slash paths a real redirects file
    uses, splitting off the query and fragment is behaviourally identical.
    Exotic inputs (dot segments, doubled slashes, characters needing
    percent-encoding) could diverge; a downstream suite comparing the replica
    against production would surface them as a parse difference.
    """
    if not path.startswith("/"):
        path = f"/{path}"
    parts = urlsplit(path)
    result = parts.path
    if include_search and parts.query:
        result += f"?{parts.query}"
    if include_hash and parts.fragment:
        result += f"#{parts.fragment}"
    return result


def _validate_url(
    token: str,
    only_relative: bool = False,
    disallow_ports: bool = False,
    include_search: bool = False,
    include_hash: bool = False,
) -> tuple[str | None, str | None]:
    host = URL_REGEX.match(token)
    if host and host["host"]:
        if only_relative:
            return (
                None,
                f"Only relative URLs are allowed. Skipping absolute URL {token}.",
            )
        if disallow_ports and HOST_WITH_PORT_REGEX.match(host["host"]):
            return (
                None,
                f"Specifying ports is not supported. Skipping absolute URL {token}.",
            )
        pathname = _extract_pathname(host["path"], include_search, include_hash)
        return f"https://{host['host']}{pathname}", None
    if not token.startswith("/") and only_relative:
        token = f"/{token}"
    if token.startswith("/"):
        return _extract_pathname(token, include_search, include_hash), None
    return None, (
        "URLs should begin with a forward-slash."
        if only_relative
        else "URLs should either be relative, or use HTTPS."
    )


def _url_has_host(token: str) -> bool:
    match = URL_REGEX.match(token)
    return bool(match and match["host"])


def parse_redirects(text: str) -> ParseResult:
    """The exact algorithm of ``parseRedirects``, budget accounting included."""
    result = ParseResult()
    seen_sources: set[str] = set()
    static_rules = 0
    dynamic_rules = 0
    can_create_static_rule = True

    lines = text.split("\n")
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > MAX_LINE_LENGTH:
            result.invalid.append(
                Invalid(
                    f"Ignoring line {index + 1} as it exceeds the maximum allowed "
                    f"length of {MAX_LINE_LENGTH}."
                )
            )
            continue

        tokens = INLINE_COMMENT_REGEX.sub("", line).split()
        if not 2 <= len(tokens) <= 3:
            result.invalid.append(
                Invalid(
                    f"Expected exactly 2 or 3 whitespace-separated tokens. "
                    f"Got {len(tokens)}.",
                    line,
                    index + 1,
                )
            )
            continue

        str_from, str_to = tokens[0], tokens[1]
        str_status = tokens[2] if len(tokens) == 3 else "302"

        source, error = _validate_url(str_from, True, True, False, False)
        if source is None:
            result.invalid.append(Invalid(error or "", line, index + 1))
            continue

        # The accounting. This is the part that decides which rules exist at all.
        if (
            can_create_static_rule
            and not SPLAT_REGEX.search(source)
            and not PLACEHOLDER_REGEX.search(source)
        ):
            static_rules += 1
            if static_rules > MAX_STATIC_RULES:
                result.invalid.append(
                    Invalid(
                        f"Maximum number of static rules supported is "
                        f"{MAX_STATIC_RULES}. Skipping line."
                    )
                )
                continue
        else:
            dynamic_rules += 1
            can_create_static_rule = False
            if dynamic_rules > MAX_DYNAMIC_RULES:
                result.invalid.append(
                    Invalid(
                        f"Maximum number of dynamic rules supported is "
                        f"{MAX_DYNAMIC_RULES}. Skipping remaining "
                        f"{len(lines) - index} lines of file."
                    )
                )
                result.aborted_at_line = index + 1
                break

        destination, error = _validate_url(str_to, False, False, True, True)
        if destination is None:
            result.invalid.append(Invalid(error or "", line, index + 1))
            continue

        # The reference reads the status with JavaScript's `Number()`, which
        # also takes `301.0` and `0x12d` for 301. This reads plain integers
        # only, so it refuses those two spellings where the engine does not.
        try:
            status = int(str_status)
        except ValueError:
            status = -1
        if status not in PERMITTED_STATUS_CODES:
            result.invalid.append(
                Invalid(
                    f"Valid status codes are 200, 301, 302 (default), 303, 307, "
                    f"or 308. Got {str_status}.",
                    line,
                    index + 1,
                )
            )
            continue

        # The engine refuses rules whose destination would re-trigger themselves
        # through its own .html / /index stripping.
        has_relative_path = not _url_has_host(destination)
        to_index = bool(INDEX_DESTINATION_REGEX.search(destination))
        wildcard_to_index = source.endswith("/*") and to_index
        root_to_index = source.endswith("/") and to_index
        if has_relative_path and (wildcard_to_index or root_to_index):
            result.invalid.append(
                Invalid("Infinite loop detected in this rule.", line, index + 1)
            )
            continue

        if source in seen_sources:
            result.invalid.append(
                Invalid(f"Ignoring duplicate rule for path {source}.", line, index + 1)
            )
            continue
        seen_sources.add(source)

        if status == 200 and _url_has_host(destination):
            result.invalid.append(
                Invalid(
                    f"Proxy (200) redirects can only point to relative paths. "
                    f"Got {destination}",
                    line,
                    index + 1,
                )
            )
            continue

        result.rules.append(Rule(source, destination, status, index + 1))

    return result


def misordered_statics(rules: list[Rule]) -> list[Rule]:
    """Exact-source rules charged against the dynamic budget by their position.

    The engine's static budget (2000) only covers exact rules appearing before
    the first dynamic source; every exact rule after that point burns a slot of
    the dynamic budget (100) instead. Such a file still works while the budget
    holds, so this is the early warning: each rule returned here brings the
    file one line closer to the silent abort {func}`parse_redirects` reports as
    ``aborted_at_line``. The fix is always the same reorder, all exact rules
    first, all pattern rules second.

    ```{caution}
    The reorder changes what the site answers for each rule that
    {func}`shadowed_statics` returns: such a rule is dead where it sits, and
    it starts to fire once it moves above the patterns. Every other rule
    answers the same before and after.
    ```
    """
    first_dynamic = next(
        (index for index, rule in enumerate(rules) if rule.is_dynamic), None
    )
    if first_dynamic is None:
        return []
    return [rule for rule in rules[first_dynamic:] if not rule.is_dynamic]


@cache
def rule_pattern(source: str) -> re.Pattern[str]:
    """Compile a rule source exactly the way ``generateRuleRegExp`` does.

    Memoized: {func}`apply_rule` recompiles the same dynamic rules once per
    probed path otherwise.
    """
    # Escape everything, turning each splat into its capture group along the way.
    pattern = "(?P<splat>.*)".join(
        ESCAPE_REGEX_CHARACTERS.sub(r"\\\g<0>", part) for part in source.split("*")
    )
    # Placeholders were escaped as-is (`:name` contains no escaped characters), so
    # they can be swapped for their capture groups after the fact.
    for name in dict.fromkeys(PLACEHOLDER_REGEX.findall(pattern)):
        pattern = pattern.replace(name, f"(?P<{name[1:]}>[^/]+)")
    return re.compile(f"^{pattern}$")


def apply_rule(rule: Rule, path: str) -> str | None:
    """Return the destination for ``path``, or None if the rule does not match.

    A rule whose source cannot compile matches nothing: the server catches the
    error and drops the rule. See {attr}`Rule.compiles`.
    """
    if not rule.compiles:
        return None
    match = rule_pattern(rule.source).match(path)
    if match is None:
        return None
    destination = rule.destination
    for name, value in match.groupdict().items():
        destination = destination.replace(f":{name}", value or "")
    return destination


def sample_path(source: str) -> str:
    """A concrete request path a rule source would have matched.

    An exact source is already one, and is returned untouched, which is the
    case that matters: a dropped exact rule names the very URL that stops
    working. A pattern has no single answer, so each `:name` stands in for
    itself and each `*` for one segment, yielding an illustration rather than
    a promise about live traffic.

    :param source: Rule source, exact or patterned.
    :return: A path that {func}`rule_pattern` would match.
    """
    concrete = PLACEHOLDER_REGEX.sub(lambda match: match.group()[1:], source)
    return concrete.replace("*", "sample")


def discarded_rules(text: str, parsed: ParseResult) -> list[Rule]:
    """The rules the engine abandoned, recovered from the tail it never read.

    {func}`parse_redirects` reports *that* it stopped and drops everything
    below, because that is what production does. Naming what was lost needs
    the tail parsed on its own, which is what this does, with the line numbers
    shifted back to where they sit in the real file.

    ```{caution}
    The tail is parsed with fresh budgets, so one long enough to exhaust them
    again reports only its first batch. Reading this as an illustration of
    what broke rather than an exhaustive inventory is the intent either way:
    the fix is the same reorder however many rules are below the line.
    ```

    :param text: The full `_redirects` source.
    :param parsed: What {func}`parse_redirects` made of it.
    :return: The abandoned rules, empty when the parser read the whole file.
    """
    if parsed.aborted_at_line is None:
        return []
    offset = parsed.aborted_at_line - 1
    tail = "\n".join(text.split("\n")[offset:])
    return [
        replace(rule, line_number=rule.line_number + offset)
        for rule in parse_redirects(tail).rules
    ]


def evaluate(rules: list[Rule], path: str) -> tuple[Rule, str] | None:
    """First-match evaluation over the kept rules, the way the asset server runs it.

    The server reads a map of exact sources first, then walks the other rules
    in file order and takes the first match. The map holds only the exact rules
    above the first dynamic one. So one pass in file order gives the same
    answer: every rule of the map sits above every rule of the walk.

    ```{warning}
    An exact rule does not win over a pattern "wherever it sits". Below the
    first dynamic rule it is one more rule of the walk, and a pattern above it
    that matches the same path answers first. {func}`shadowed_statics` lists
    the exact rules that never fire for that reason.
    ```
    """
    for rule in rules:
        destination = (
            apply_rule(rule, path)
            if rule.is_dynamic
            else (rule.destination if rule.source == path else None)
        )
        if destination is not None:
            return rule, destination
    return None


def shadowed_statics(rules: list[Rule]) -> list[tuple[Rule, Rule]]:
    """Exact rules that never fire, each with the pattern that answers for it.

    An exact rule below the first dynamic one is probed in file order with the
    patterns (see {func}`evaluate`). When a pattern above it matches its source,
    that pattern answers every request the exact rule was written for. Nothing
    reports it: the request still redirects, to the destination of the pattern.

    :param rules: The rules {func}`parse_redirects` kept.
    :return: Pairs of the dead exact rule and the rule that fires in its place.
    """
    shadowed = []
    for rule in misordered_statics(rules):
        landing = evaluate(rules, rule.source)
        if landing is not None and landing[0] is not rule:
            shadowed.append((rule, landing[0]))
    return shadowed
