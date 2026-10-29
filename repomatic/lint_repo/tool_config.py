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

"""Checks of the tool configurations `repomatic init` writes into `pyproject.toml`.

A section `init` seeds once and never revisits misses the knobs its template
gains later. A section synced on every run can keep a bare entry beside the
anchored form that replaced it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

import tomlrt

from ..bundle import get_data_content
from ..checks import CheckResult
from ..components import SyncMode

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import Any

    from .context import LintContext


def missing_template_entries(
    template: Mapping[str, Any],
    local: Mapping[str, Any],
    prefix: str = "",
) -> Iterator[tuple[str, str | None]]:
    """Yield the dotted paths a seeded tool section does not carry.

    Absence is the only thing reported. A scalar the repository tuned is its
    own answer, a key it added is none of the template's business, and a
    comment it rewrote is invisible here: all three are what
    {attr}`~repomatic.components.SyncMode.BOOTSTRAP` hands over on the first
    write. What that contract cannot express is a knob the template gained
    *afterwards*, which reaches nobody because `init` never revisits the
    section.

    A list is compared by membership, not by equality. An ignore list or a
    select list grows one entry at a time and its order carries no meaning, so
    equality would report every local addition as drift.

    :param template: The bundled template, parsed.
    :param local: The repository's own section, parsed.
    :param prefix: Dotted path of the table being walked, for recursion.
    :return: `(path, None)` per missing key, `(path, member)` per missing list
        member, named by {func}`template_member_name`.
    """
    for key, expected in template.items():
        path = f"{prefix}{key}"
        if key not in local:
            yield path, None
            continue
        found = local[key]
        if isinstance(expected, Mapping) and isinstance(found, Mapping):
            yield from missing_template_entries(expected, found, f"{path}.")
        elif isinstance(expected, list) and isinstance(found, list):
            for item in expected:
                if item not in found:
                    yield path, template_member_name(item)


def template_member_name(member: object) -> str:
    """Name one member of a template list the way a `lint-repo` line reads it.

    A scalar is its own name. A table has no text of its own, so its name is
    the inline table that writes it, like `{ fruit = "apple", ripe = true }`.
    That is the text a maintainer copies into the section, or quotes as the key
    of a {attr}`~repomatic.config.LintRepoConfig.declined` entry.

    :param member: One item of a list held by a bundled template.
    :return: The member as text, or as inline TOML when it is a table.
    """
    if not isinstance(member, Mapping):
        return str(member)
    plain = member.to_dict() if isinstance(member, tomlrt.Table) else dict(member)
    # tomlrt renders a value only as part of a document, so the table is
    # written under a throwaway key that is then cut off.
    rendered = tomlrt.dumps({"member": tomlrt.Table.inline(plain)})
    return rendered.removeprefix("member = ").rstrip("\n")


_BARE_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")
"""A TOML key that needs no quotes."""


def _dotted_toml_key(path: Iterable[str]) -> str:
    """Render a key path as the dotted TOML key that names it."""
    return ".".join(
        part if _BARE_TOML_KEY.fullmatch(part) else json.dumps(part, ensure_ascii=False)
        for part in path
    )


def _template_entry(path: str, member: str | None) -> str:
    """Name one bundled template entry the way a `lint-repo` line reads it."""
    return f"`{path}`" if member is None else f'`{path}` member "{member}"'


def check_bootstrap_config_drift(ctx: LintContext) -> Iterator[CheckResult]:
    """Report bundled knobs a seeded tool section never received.

    `init` writes the template of a
    {attr}`~repomatic.components.SyncMode.BOOTSTRAP` component once and hands
    the section over, so the repository owns every value from then on. That is
    deliberate, and this check does not argue with it. The gap it closes is
    narrower: a template that gains a knob *after* the seeding has no route into
    the repository and nothing that mentions the difference, so the section
    silently stops being the configuration repomatic ships.

    Advisory, and deliberately so. A repository may have dropped a template
    rule on purpose, and only its maintainer can tell that from a section left
    behind.

    The maintainer records that answer in
    {attr}`~repomatic.config.LintRepoConfig.declined`. A declined entry is
    reported as skipped, with its reason: the decision stays in the report and
    the warning stops. The record is checked like the section it speaks for.
    An entry that declines nothing is reported, since the next gap at its path
    would otherwise arrive already declined.

    A section keeping its keys in the component's
    {attr}`~repomatic.components.ToolConfigComponent.legacy_table` is measured
    there, and the finding names that sub-table. An entry is declined at its
    path below the tool either way, without the sub-table.

    :return: One result per declined entry and one per adopted BOOTSTRAP
        section, then one per recorded entry that declines nothing.
    """
    tool_table = ctx.tool_table
    declined = ctx.config.lint_repo.declined_entries()
    seeded = {comp.tool_name: comp for comp in SyncMode.BOOTSTRAP.components()}
    # Tools whose section was compared with its template, and tools whose
    # template could not be read.
    measured: set[str] = set()
    unreadable: set[str] = set()
    # Entries that took a finding out of the report, and entries that only
    # lack a reason to do so.
    applied: set[tuple[str, ...]] = set()
    unreasoned: set[tuple[str, ...]] = set()
    # The members still reported under each list the section carries, keyed
    # like an entry naming the list itself.
    loose_members: dict[tuple[str, ...], list[str]] = {}

    for comp in seeded.values():
        local = tool_table.get(comp.tool_name)
        if not isinstance(local, Mapping):
            continue
        section = comp.tool_section
        legacy = local.get(comp.legacy_table) if comp.legacy_table else None
        if isinstance(legacy, Mapping):
            local = legacy
            section = f"{section}.{comp.legacy_table}"

        try:
            template = tomlrt.loads(get_data_content(comp.source_file))
        except (OSError, ValueError, tomlrt.TOMLParseError):
            unreadable.add(comp.tool_name)
            yield CheckResult(
                None,
                f"[{section}] drift: skipped (bundled"
                f" `{comp.source_file}` could not be read).",
            )
            continue
        measured.add(comp.tool_name)

        missing: list[tuple[str, str | None]] = []
        declines = False
        for path, member in missing_template_entries(template, local):
            if (path, member) in comp.customizable_entries:
                continue
            entry: tuple[str, ...] = (comp.tool_name, *path.split("."))
            if member is not None:
                entry = (*entry, member)
            reason = declined.get(entry)
            if isinstance(reason, str) and reason.strip():
                applied.add(entry)
                declines = True
                yield CheckResult(
                    None,
                    f"[{section}] declines {_template_entry(path, member)}:"
                    f" {reason.strip()}",
                )
                continue
            if entry in declined:
                unreasoned.add(entry)
            if member is not None:
                loose_members.setdefault(entry[:-1], []).append(member)
            missing.append((path, member))

        if missing:
            listed = ", ".join(_template_entry(*found) for found in missing)
            yield CheckResult(
                False,
                f"[{section}] does not carry"
                f" {listed} from the"
                f" bundled `{comp.source_file}`. `init` seeds this section"
                f" once and never revisits it, so a knob the template gained"
                f" since arrives only by hand. Copy what applies, and name what"
                f" stays out under `[tool.repomatic] lint-repo.declined` with"
                f" its reason.",
            )
            continue

        scope = "every other entry" if declines else "every entry"
        yield CheckResult(
            True,
            f"[{section}] carries {scope} of the bundled `{comp.source_file}`.",
        )

    for entry in declined:
        tool = entry[0]
        if entry in applied or tool in unreadable:
            continue
        named = (
            f"`[tool.repomatic] lint-repo.declined` entry `{_dotted_toml_key(entry)}`"
        )
        owner = seeded.get(tool)
        if entry in unreasoned:
            verdict = (
                "gives no reason, so the entry it names stays reported. Map it"
                " to the reason that entry stays out."
            )
        elif owner is None:
            verdict = (
                f"declines nothing: `{tool}` is not a tool config `init` seeds"
                f" once. Remove the entry."
            )
        elif tool not in measured:
            verdict = (
                f"declines nothing: the repository has no"
                f" `[{owner.tool_section}]` section. Remove the entry."
            )
        elif entry in loose_members:
            # The section gained the list after the entry was written, so the
            # gap is a member of it now, and so is the key that declines it.
            keys = ", ".join(
                f"`{_dotted_toml_key((*entry, member))}`"
                for member in loose_members[entry]
            )
            verdict = (
                f"declines nothing: `[{owner.tool_section}]` carries"
                f" `{'.'.join(entry[1:])}`, and an entry declines one of its"
                f" members by name: {keys}. Move the reason to that key."
            )
        else:
            verdict = (
                f"declines nothing: `[{owner.tool_section}]` carries what it"
                f" names, or the bundled `{owner.source_file}` does not. Remove"
                f" the entry."
            )
        yield CheckResult(False, f"{named} {verdict}")


_ANCHORED_EXCLUDE_HEAD = re.compile(r"^\^https://\(www\\\.\)\?")
"""The `^https://(www\\.)?` head of an anchored `[tool.lychee] exclude` entry."""


_ANCHORED_EXCLUDE_TAIL = re.compile(r"\(/\.\*\)\?\$$")
"""The `(/.*)?$` tail of an anchored `[tool.lychee] exclude` entry."""


_EXCLUDE_PATTERN_TOOLS = frozenset({"lychee"})
"""Synced tool configs whose array holds regex patterns rather than plain values.

{func}`check_superseded_local_excludes` reads a bare entry as the unanchored
form of an anchored one, which means something only for a list of patterns
matched against URLs. Every other synced array holds values with no
anchored/bare distinction, so the check stays out of them.
"""


def anchored_exclude_core(pattern: str) -> str | None:
    """Return the bare host pattern an anchored exclude entry supersedes.

    `^https://(www\\.)?x\\.com(/.*)?$` answers `x\\.com`. Anything else answers
    `None`: an entry not anchored on both ends replaced no bare form, and one
    carrying a path between the host and the closing anchor
    (`^https://www\\.npmjs\\.com/package/.*$`) has no bare form that was ever
    bundled on its own.

    :param pattern: One `[tool.lychee] exclude` entry.
    :return: The unanchored host pattern, or `None` when there is none.
    """
    if not _ANCHORED_EXCLUDE_HEAD.match(pattern):
        return None
    core = _ANCHORED_EXCLUDE_HEAD.sub("", pattern)
    if not _ANCHORED_EXCLUDE_TAIL.search(core):
        return None
    return _ANCHORED_EXCLUDE_TAIL.sub("", core)


def check_superseded_local_excludes(ctx: LintContext) -> Iterator[CheckResult]:
    """Report a local exclude entry the bundled template has since anchored.

    An ongoing sync rebuilds a section from its template and grafts local-only
    content back on, and for an array the graft is a union by value: an item
    the template does not carry is appended after the ones it does. That is
    what keeps a repository's own entries alive across a sync, and it has no
    inverse. When the template *replaces* an entry instead of adding one, the
    old form becomes local-only by the next sync, so it is grafted back beside
    its own replacement and nothing ever retires it.

    Anchoring a bare host pattern is exactly that replacement, and the stale
    copy is not inert: lychee reads these entries as substring matches, so a
    bare `x\\.com` kept beside `^https://(www\\.)?x\\.com(/.*)?$` goes on
    excluding every domain ending in `x.com`, `cloudlinux.com` among them,
    which is what the anchored form was written to stop doing.

    The fix depends on what sits beside the stale entry. Where the anchored
    form is present, dropping the bare one is the whole fix. A section no sync
    reaches holds the bare form alone: lychee is an awesome-only component, so
    another repository keeps a copy nothing rebuilds unless
    `[tool.repomatic] include` opts it in. Dropping the entry there would stop
    excluding the host itself, so the result names the anchored form to write
    in its place.

    Advisory. Only the maintainer can separate a bare entry the template
    superseded from one the repository narrowed on purpose.

    ```{note}
    The test is deliberately narrow: it recognises the anchored/bare pair by
    string equality against the head and tail above, so it catches a template
    that anchored a host pattern and nothing else. A local entry broadened in
    some other way is not reported. Widening it means inverting a regex to
    compare match sets, which is not worth a dependency for one signature that
    already reached every repository the bundled `lychee.toml` deploys to.
    ```

    :return: One result per synced tool config holding exclude patterns.
    """
    for comp in SyncMode.ONGOING.components():
        if comp.tool_name not in _EXCLUDE_PATTERN_TOOLS:
            continue
        section = ctx.tool_table.get(comp.tool_name)
        if not isinstance(section, Mapping):
            continue
        local = section.get("exclude")
        if not isinstance(local, list):
            continue

        try:
            template = tomlrt.loads(get_data_content(comp.source_file))
        except (OSError, ValueError, tomlrt.TOMLParseError):
            yield CheckResult(
                None,
                f"[{comp.tool_section}] exclude drift: skipped (bundled"
                f" `{comp.source_file}` could not be read).",
            )
            continue

        bundled = template.get("exclude")
        if not isinstance(bundled, list):
            yield CheckResult(
                None,
                f"[{comp.tool_section}] exclude drift: skipped (bundled"
                f" `{comp.source_file}` declares no `exclude` array).",
            )
            continue
        anchors = {
            core: str(item)
            for item, core in (
                (item, anchored_exclude_core(str(item))) for item in bundled
            )
            if core is not None
        }
        stale = tuple(
            item
            for item in local
            if isinstance(item, str)
            and item not in bundled
            and not item.startswith("^")
            and item in anchors
        )
        if stale:
            listed = ", ".join(f"`{item}`" for item in stale)
            fixes = []
            for item in stale:
                anchored = anchors[item]
                if anchored in local:
                    fixes.append(
                        f"Drop `{item}`: `{anchored}` beside it already"
                        f" excludes the host, and an ongoing sync never"
                        f" retires the bare form."
                    )
                else:
                    fixes.append(
                        f"Replace `{item}` with `{anchored}`: the section"
                        f" lacks the anchored form, so dropping the bare one"
                        f" would stop excluding the host itself."
                    )
            yield CheckResult(
                False,
                f"[{comp.tool_section}] exclude carries {listed}, which the"
                f" bundled `{comp.source_file}` now anchors: lychee reads"
                f" these as substring matches, so a bare form keeps excluding"
                f" every domain ending in the same suffix. {' '.join(fixes)}",
            )
            continue

        yield CheckResult(
            True,
            f"[{comp.tool_section}] exclude carries no entry the bundled"
            f" `{comp.source_file}` superseded.",
        )
