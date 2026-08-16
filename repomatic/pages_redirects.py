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

Transcribed on 2026-08-10 from the engine itself, not from the documentation:

- Parsing: ``packages/workers-shared/utils/configuration/parseRedirects.ts`` in
  [cloudflare/workers-sdk](https://github.com/cloudflare/workers-sdk), as
  bundled in wrangler 4.118 (the same code path Miniflare uses, and the same
  parser family the Pages asset server feeds on).
- Matching: ``packages/workers-shared/asset-worker/src/utils/rules-engine.ts``.

The three rules of the engine that the documentation does not state:

1. **A static rule is only free while it appears before the first dynamic
   rule.** The parser flips ``canCreateStaticRule`` to false permanently at the
   first source containing ``*`` or ``:placeholder``. Every later rule, however
   static it looks, is charged against the dynamic budget.
2. **The dynamic budget is 100, and blowing it aborts the file.** Rule 101 of
   that mixed stream does not get skipped: the parser ``break``s, discarding
   every remaining line. Order is therefore not a style choice, it decides
   which rules exist.
3. **Matching is anchored and literal about trailing slashes.** A placeholder
   compiles to ``[^/]+`` (at least one character, never a slash, never empty),
   a splat to ``.*`` (may be empty), and the whole source to ``^...$``.
   ``/a/:b`` does not match ``/x/y/`` and ``/a/*`` matches ``/a/`` with an
   empty splat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

MAX_LINE_LENGTH = 2000
MAX_STATIC_RULES = 2000
MAX_DYNAMIC_RULES = 100
PERMITTED_STATUS_CODES = frozenset({200, 301, 302, 303, 307, 308})

SPLAT_REGEX = re.compile(r"\*")
PLACEHOLDER_REGEX = re.compile(r":[A-Za-z]\w*")
URL_REGEX = re.compile(r"^https://+(?P<host>[^/]+)/?(?P<path>.*)")
HOST_WITH_PORT_REGEX = re.compile(r".*:\d+$")

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

        # The engine strips inline comments: whitespace followed by `#` ends the rule.
        tokens = re.sub(r"\s+#.*$", "", line).split()
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
        wildcard_to_index = source.endswith("/*") and re.search(
            r"/index(.html)?$", destination
        )
        root_to_index = source.endswith("/") and re.search(
            r"/index(.html)?$", destination
        )
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
    first, all pattern rules second, which is behaviour-preserving because the
    asset server probes exact sources first regardless of file position.
    """
    first_dynamic = next(
        (index for index, rule in enumerate(rules) if rule.is_dynamic), None
    )
    if first_dynamic is None:
        return []
    return [rule for rule in rules[first_dynamic:] if not rule.is_dynamic]


def rule_pattern(source: str) -> re.Pattern[str]:
    """Compile a rule source exactly the way ``generateRuleRegExp`` does."""
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
    """Return the destination for ``path``, or None if the rule does not match."""
    match = rule_pattern(rule.source).match(path)
    if match is None:
        return None
    destination = rule.destination
    for name, value in match.groupdict().items():
        destination = destination.replace(f":{name}", value or "")
    return destination


def evaluate(rules: list[Rule], path: str) -> tuple[Rule, str] | None:
    """First-match evaluation over the kept rules, the way the asset server runs it.

    The server splits exact sources into a hash map probed first, then walks
    the dynamic rules in file order. The two passes below mirror that: an
    exact rule wins over a pattern that also matches, wherever each sits in
    the file, which is also what makes the statics-first reorder the lint
    recommends behaviour-preserving.
    """
    for rule in rules:
        if not rule.is_dynamic and rule.source == path:
            return rule, rule.destination
    for rule in rules:
        if rule.is_dynamic:
            destination = apply_rule(rule, path)
            if destination is not None:
                return rule, destination
    return None
