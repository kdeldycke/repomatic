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

"""Merge the bundled `[tool.*]` templates into a `pyproject.toml`.

Each {class}`~repomatic.components.ToolConfigComponent` ships its section in
the native format of its tool, under `repomatic/data/`. A missing section is
installed from the template. An existing one is left alone, or re-derived from
the template on every sync while the local additions are grafted back on,
depending on its {class}`~repomatic.components.SyncMode`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import tomlrt

from ..bundle import get_data_content
from ..components import (
    COMPONENTS,
    InitDefault,
    SyncMode,
    ToolConfigComponent,
)
from ..pyproject import read_pyproject_toml

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

PYPROJECT_FMT_INDENT = 2
"""Spaces `pyproject-fmt` puts before each row of a multi-line array, its default.

`_graft_local_additions` lays out a rebuilt multi-line array at this indent.
tomlrt's own fallback is four spaces, which the `format-pyproject` job rewrites
in a pull request of its own.
"""

TOOL_CONFIG_COMPONENTS: dict[str, ToolConfigComponent] = {
    comp.name: comp for comp in COMPONENTS if isinstance(comp, ToolConfigComponent)
}
"""The tool config components, by name: what {func}`init_config` accepts."""

_MISSING = object()
"""Sentinel marking a key absent from the template.

A legitimate template value of `None` is then not mistaken for "missing"
during the merge walk.
"""


def _strip_header_comments(native_source: str) -> str:
    """Drop the file-level header block from a native-format template.

    Templates open with header comments explaining the standalone format
    (`bumpversion.toml` and the others); those do not belong in the `[tool.X]`
    section grafted into `pyproject.toml`. Everything below them does, per-key
    comments included: they are the only documentation a downstream
    `pyproject.toml` ever carries for a key it did not write.

    The header is the leading comment run **terminated by a blank line**, and
    the blank goes with it. A leading comment run running straight into a key
    belongs to that key and is kept, which is what separates the two shapes a
    template may open with:

    ```toml
    # Header, blank line below.      | # Header, key right below.
                                     | preview = true
    # Comment owned by the key.      |
    default.extend-identifiers = {}  |
    ```

    Skipping to the first non-comment line instead would swallow the second
    comment along with the header, and leave the first key of the template
    undocumented downstream. The blank-line convention is pinned by
    `test_native_templates_separate_header`.

    ```{note}
    Use `splitlines(keepends=True)`, not `"\\n".join(splitlines(...))`: the
    template's trailing newline carries over to the final KV slot's EOL
    trivia, and a synced section that happens to land at end-of-document
    needs that newline to survive so the rendered file ends with `\\n`. The
    invariant is pinned by `test_update_tool_config_output_contract`.
    ```
    """
    lines = native_source.splitlines(keepends=True)
    header_end = 0
    while header_end < len(lines) and lines[header_end].lstrip().startswith("#"):
        header_end += 1
    # No blank line below means the run documents the first key, not the file.
    if header_end >= len(lines) or lines[header_end].strip():
        return native_source
    while header_end < len(lines) and not lines[header_end].strip():
        header_end += 1
    return "".join(lines[header_end:])


def _template_section(comp: ToolConfigComponent) -> tomlrt.Document:
    """Parse the bundled template of *comp*, without its file-level header.

    The template is kept as a parsed `Document`, not lifted via
    `Table.section()`: installing or assigning a `Document` routes through
    tomlrt's trivia-preserving clone path
    ([dimbleby/tomlrt#171](https://github.com/dimbleby/tomlrt/issues/171)),
    so standalone comments above scalar keys and inline-array bracket pad carry
    over with the values.
    """
    return tomlrt.loads(_strip_header_comments(get_data_content(comp.source_file)))


def _entry_identity(
    entry: Mapping[str, Any], identity_keys: tuple[str, ...]
) -> tuple[tuple[str, Any], ...]:
    """Return the slot identity of an array-of-tables entry.

    The identity is the tuple of `(key, value)` pairs for the `identity_keys`
    the entry actually carries, so two entries map to the same slot when they
    agree on every identity key they share. See
    {attr}`~repomatic.components.ToolConfigComponent.graft_identity_keys`.
    """
    return tuple((key, entry[key]) for key in identity_keys if key in entry)


def _carry_comments(
    source: Any, source_slot: str | int, target: Any, target_slot: str | int
) -> None:
    """Copy the comments beside one slot of *source* onto one slot of *target*.

    Both containers are tomlrt tables or arrays. A value grafted from one
    arrives bare: the comment block above it and the comment after it belong
    to the container's layout, not to the value. tomlrt keeps both in views
    keyed by slot, the key of a table entry or the position of an array item.
    Writing a comment on an item turns a single-line *target* array into one
    item per line, since a comment needs a line of its own.

    tomlrt writes an end-of-line comment one space after its value, where
    `pyproject-fmt` puts two. {func}`_same_modulo_comment_gaps` keeps that
    difference from reading as a change.
    """
    block = source.leading_block.get(source_slot)
    if block:
        target.leading_block[target_slot] = block
    trailing = source.comments.get(source_slot)
    if trailing:
        target.comments[target_slot] = trailing


def _same_modulo_comment_gaps(first: str, second: str) -> bool:
    """Whether two renderings differ at most in the spaces before a `#`.

    {func}`_carry_comments` writes an end-of-line comment one space after its
    value, and `pyproject-fmt` moves it to two. Compared exactly, a section
    carrying such a comment reads as changed on every sync, so the unattended
    `sync-repomatic` and `format-pyproject` jobs would each open a pull request
    undoing the other's. A `#` inside a string value is normalized too, which
    can only hide a change made of nothing but the spaces before that `#`.
    """
    gap = re.compile(r"[ \t]+#")
    return gap.sub(" #", first).strip() == gap.sub(" #", second).strip()


def _graft_local_additions(
    target: Any,
    template: Mapping[str, Any],
    existing: Mapping[str, Any],
    identity_keys: tuple[str, ...] = (),
) -> None:
    """Graft local-only content from an existing section onto a template table.

    Walks the *existing* tomlrt section against the *template* (a plain-dict
    view) and copies into *target* (the tomlrt table built from the template)
    every piece of local configuration the canonical template does not already
    carry:

    - **Keys the template does not define** are grafted verbatim, so a
      project-specific table like `[tool.typos.type.po]` survives untouched.
    - **Tables present in both** are merged recursively, so local keys inside a
      table the template also defines are kept (like a downstream entry in
      `[tool.typos.default.extend-identifiers]` next to the canonical ones).
    - **Arrays present in both** gain their local-only items appended after the
      template items. Scalar arrays (`extend-ignore-re`, `serialize`) and
      arrays-of-tables with no `identity_keys` use a plain union by value.
      When `identity_keys` is set, an array-of-tables entry that shares a slot
      identity with a template entry (`[[tool.bumpversion.files]]` whose
      canonical `search` has evolved) is dropped in favour of the template
      instead of appended as a duplicate.
    - **Scalars present in both** are left as the template defines them: the
      canonical value wins, which is the point of an ongoing sync.

    Grafted keys are copied from *existing* as nodes, so the comments and
    inline formatting inside them carry over. The comments beside a grafted key
    belong to the parent table's layout, so {func}`_carry_comments` copies them
    across. A local-only array item is appended as a value instead: its
    comments are copied the same way, but its lexeme is re-emitted in tomlrt's
    own style. An array the project wrote one item per row keeps that layout, at
    {data}`PYPROJECT_FMT_INDENT`. A comment beside a key the template also
    defines is not carried: the template's own comment wins, like its value.

    :param target: tomlrt table built from the bundled template, mutated in
        place.
    :param template: Plain-dict view of the bundled template section.
    :param existing: tomlrt view of the local section being synced, walked for
        structure and used as the source of grafted nodes.
    :param identity_keys: Keys identifying an array-of-tables entry's slot. When
        set, a local entry sharing a slot with a template entry is dropped in
        favour of the template rather than appended as a duplicate.
    """
    for key, existing_value in existing.items():
        template_value = template.get(key, _MISSING)
        if template_value is _MISSING:
            # Key the canonical template does not define: keep it verbatim.
            target[key] = existing_value
            # The section root parses as a `Document`, which is not a `Table`.
            containers = (tomlrt.Document, tomlrt.Table)
            if isinstance(existing, containers) and isinstance(target, containers):
                _carry_comments(existing, key, target, key)
        elif isinstance(template_value, dict) and isinstance(existing_value, dict):
            # Standard or inline table: recurse so local-only sub-keys survive.
            _graft_local_additions(
                target[key],
                template_value,
                existing_value,
                identity_keys,
            )
        elif isinstance(template_value, list) and isinstance(existing_value, list):
            # Slots already claimed by the canonical template: a local entry
            # mapping to one of these is a (possibly stale) copy of a template
            # entry, so the template wins and the local copy is not re-added.
            template_slots = (
                {
                    _entry_identity(item, identity_keys)
                    for item in template_value
                    if isinstance(item, dict)
                }
                if identity_keys
                else set()
            )
            # Array in both: append local-only items, preserving their order.
            grafted = {
                index: item
                for index, item in enumerate(existing_value)
                # Same slot as a canonical entry: superseded by the template.
                if not (
                    isinstance(item, dict)
                    and _entry_identity(item, identity_keys) in template_slots
                )
                and item not in template_value
            }
            if list(existing_value) == [*template_value, *grafted.values()]:
                # The merge changes nothing, so keep the array the project
                # wrote instead of rebuilding an equal one. Rebuilding re-emits
                # every item in tomlrt's own style, because iterating an array
                # yields decoded values rather than the nodes carrying their
                # source lexeme: a literal `'base32 "[0-9a-z]{52}"'` comes back
                # as an escaped `"base32 \"[0-9a-z]{52}\""`. Same content, and
                # `pyproject-fmt` rewrites it straight back, so the unattended
                # `sync-repomatic` and `format-pyproject` jobs each open a pull
                # request undoing the other's, forever.
                target[key] = existing_value
                continue
            if (
                isinstance(existing_value, tomlrt.Array)
                and isinstance(target[key], tomlrt.Array)
                and existing_value.multiline
                and not target[key].multiline
            ):
                # Keep the one-row-per-item layout the project wrote. On one
                # line, tomlrt breaks the array into rows itself when a carried
                # comment needs a line of its own, but at four spaces.
                target[key].set_multiline(multiline=True, indent=PYPROJECT_FMT_INDENT)
            for index, item in grafted.items():
                target[key].append(item)
                if isinstance(existing_value, tomlrt.Array) and isinstance(
                    target[key], tomlrt.Array
                ):
                    _carry_comments(
                        existing_value, index, target[key], len(target[key]) - 1
                    )
        # Scalar in both: the canonical template value wins, nothing to graft.


def _overlay_owned_keys(existing_section: Any, comp: ToolConfigComponent) -> None:
    """Update only the template's keys in place within an existing section.

    The overlay sync for a partial template ({attr}`ToolConfigComponent.overlay`):
    the template owns a few policy keys inside an otherwise project-owned
    section. Each template key's value is written into the existing section,
    updating it in place when present (key position and surrounding trivia
    preserved) or appending it when absent. Every other key the project defines
    is left untouched, so the merged section keeps its order and stays a
    `pyproject-fmt` fixpoint. This is the inverse of the rebuild-and-graft path:
    there the template is the base and local keys graft on; here the existing
    section is the base and the template overlays its owned keys.

    :param existing_section: The live `[tool.X]` table being overlaid, mutated
        in place.
    :param comp: The component whose owned keys are being synced.
    """
    for key, value in _template_section(comp).to_dict().items():
        existing_section[key] = value


def _rebuild_section(tool_table: Any, comp: ToolConfigComponent) -> None:
    """Re-derive an existing `[tool.X]` section from the bundled template.

    Rebuilds the section from the canonical template, then grafts local-only
    configuration back on via {func}`_graft_local_additions`: keys the template
    omits, extra items in shared arrays, and extra keys in shared nested
    tables. The template wins on shared scalars and on array-of-tables entries
    that share a `graft_identity_keys` slot; `comp.preserved_keys` overrides
    that for named top-level keys, like `current_version`.

    :param tool_table: The `[tool]` table holding the section, mutated in place.
    :param comp: The component whose config is being synced.
    """
    existing_section = tool_table[comp.tool_name]
    new_section = _template_section(comp)
    # Graft before assigning: the assignment deep-clones `new_section`, so
    # grafted entries must be in place first to carry over with their comments.
    _graft_local_additions(
        new_section,
        new_section.to_dict(),
        existing_section,
        comp.graft_identity_keys,
    )
    # `preserved_keys` are authoritative locally: the local value overrides
    # the template placeholder.
    for key in comp.preserved_keys:
        if key in existing_section:
            new_section[key] = existing_section[key]
    tool_table[comp.tool_name] = new_section


def merge_tool_config(content: str, comp: ToolConfigComponent) -> str | None:
    """Merge the template of *comp* into the `pyproject.toml` text *content*.

    A missing section is installed from the template. An existing one is left
    alone in {attr}`~repomatic.components.SyncMode.BOOTSTRAP` mode. In
    {attr}`~repomatic.components.SyncMode.ONGOING` mode it is re-derived from
    the template, or, for a partial template
    ({attr}`~repomatic.components.ToolConfigComponent.overlay`), only the keys
    the template owns are updated in place.

    :param content: The current `pyproject.toml` content.
    :param comp: The component whose section is merged.
    :return: The new content, or `None` when the section needs no change.
    """
    doc = tomlrt.loads(content)
    tool_table = doc.get("tool")
    if not tool_table or comp.tool_name not in tool_table:
        doc.install(("tool", comp.tool_name), _template_section(comp))
        logging.info(f"Added [{comp.tool_section}] from bundled template.")
        return tomlrt.dumps(doc)

    if comp.sync_mode is SyncMode.BOOTSTRAP:
        logging.info(f"[{comp.tool_section}] already exists.")
        return None
    return _update_tool_config(doc, content, comp)


def _update_tool_config(
    doc: tomlrt.Document, content: str, comp: ToolConfigComponent
) -> str | None:
    """Re-derive the existing section of *comp* in *doc* from its template.

    The ongoing sync: {func}`_rebuild_section` rebuilds the section and grafts
    the local additions back on, or, for a partial template
    ({attr}`~repomatic.components.ToolConfigComponent.overlay`),
    {func}`_overlay_owned_keys` updates only the keys the template owns.

    :param doc: The parsed `pyproject.toml`, whose `[tool]` table holds the
        section. Mutated in place.
    :param content: The text *doc* was parsed from, for the no-op check.
    :param comp: The component whose section is synced.
    :return: The new content, or `None` when the section needs no change.
    """
    tool_table = doc["tool"]
    if comp.overlay:
        _overlay_owned_keys(tool_table[comp.tool_name], comp)
    else:
        _rebuild_section(tool_table, comp)
    modified = tomlrt.dumps(doc)
    if _same_modulo_comment_gaps(modified, content):
        logging.info(f"[{comp.tool_section}] already up to date.")
        return None
    logging.info(f"Synced [{comp.tool_section}] from bundled template.")
    return modified


def init_config(config_type: str, pyproject_path: Path | None = None) -> str | None:
    """Merge the template of one tool config into a `pyproject.toml` file.

    Reads the file and hands it to {func}`merge_tool_config`. The file itself is
    left alone: the caller writes the returned content.

    :param config_type: The component name (like `"ruff"` or `"bumpversion"`).
    :param pyproject_path: Path to `pyproject.toml`. Defaults to
        `./pyproject.toml`.
    :return: The modified `pyproject.toml` content, or `None` when no change is
        needed or the file does not exist.
    :raises ValueError: If no tool config component carries that name.
    """
    comp = TOOL_CONFIG_COMPONENTS.get(config_type)
    if comp is None:
        supported = ", ".join(TOOL_CONFIG_COMPONENTS)
        msg = f"Unknown config type: {config_type!r}. Supported: {supported}"
        raise ValueError(msg)

    if pyproject_path is None:
        pyproject_path = Path("pyproject.toml")
    if not pyproject_path.exists():
        logging.error(f"File not found: {pyproject_path}")
        return None
    return merge_tool_config(pyproject_path.read_text(encoding="UTF-8"), comp)


def adopted_ongoing_configs(output_dir: Path) -> set[str]:
    """Return the ongoing tool configs whose section `pyproject.toml` already carries.

    {attr}`~repomatic.components.InitDefault.EXPLICIT` governs *adoption*, not
    upkeep: it keeps a bare `init` from pushing `[tool.typos]` onto a repository
    that never asked for one. Once the section is there the repository has
    asked, so an {attr}`~repomatic.components.SyncMode.ONGOING` component rejoins
    the bare-init set and resumes tracking the bundled template.

    Without this the two flags cancel out. The only sync that ever runs
    unattended is the bare `init` the `sync-repomatic` job calls, so an ONGOING
    section is otherwise re-derived only when a human types its component name,
    and a `[tool.typos]` written by hand sits indefinitely beside a bundled
    template it never adopts a single rule from.

    {attr}`~repomatic.components.SyncMode.BOOTSTRAP` components stay out: their
    template is a starting point the repository owns outright after the first
    write, and re-selecting one would revert deliberate local edits.

    :param output_dir: Repository root holding `pyproject.toml`.
    :return: Component names to add to a bare `init` selection. Empty when the
        file is absent, unparsable, or carries no `[tool]` table.
    """
    tool_table = read_pyproject_toml(output_dir).get("tool", {})
    if not tool_table:
        return set()
    return {
        comp.name
        for comp in SyncMode.ONGOING.components()
        if comp.init_default is InitDefault.EXPLICIT and comp.tool_name in tool_table
    }
