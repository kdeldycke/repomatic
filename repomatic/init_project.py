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

"""Bundled data files, configuration templates, and repository initialization.

Provides a unified interface for accessing bundled data files from
`repomatic/data/` and orchestrates repository bootstrapping via
`repomatic init`.

Every component `repomatic init` accepts is declared in
{data}`~repomatic.registry.COMPONENTS`, which carries each one's description,
default scope and target paths; `repomatic init --help` lists them. That tuple
is the only roster: a list repeated here would silently fall behind it.

Selectors use the same `component[/file]` syntax as the `exclude`
config option in `[tool.repomatic]`.  Qualified entries like
`skills/repomatic-topics` select a single file within a component.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path, PurePosixPath
from shutil import rmtree

import tomlrt
import yaml

from . import __git_tag_sha__, __version__
from .bundle import get_data_content
from .config import Config, load_repomatic_config, location_path
from .github.releases import resolve_tag_to_sha
from .github.workflow_sync import (
    PathsSpec,
    extract_extra_jobs,
    generate_workflow_header,
    identify_canonical_workflow,
    render_thin_caller_for_target,
)
from .http import get_bytes
from .lint_repo import requested_metadata_keys
from .metadata import Metadata, all_metadata_keys
from .plugin import merge_plugin_settings
from .prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from .pyproject import (
    is_python_package,
    is_python_project,
    read_pyproject_toml,
    resolve_source_paths,
)
from .registry import (
    COMPONENTS,
    COMPONENTS_BY_NAME,
    DEFAULT_REPO,
    GITHUB_YAML_PATTERNS,
    NON_REUSABLE_WORKFLOWS,
    REMOVED_ASSETS,
    REUSABLE_WORKFLOWS,
    UPSTREAM_REPO_SLUGS,
    BundledComponent,
    GeneratedComponent,
    InitDefault,
    RemovedAsset,
    SyncMode,
    TemplateComponent,
    ToolConfigComponent,
    WorkflowComponent,
    excluded_rel_path,
    is_awesome_repo,
    package_of,
    parse_component_entries,
)
from .tool_registry import TOOL_REGISTRY
from .tool_runner import find_unmodified_configs
from .version_sync import (
    Candidate,
    UpstreamRefPin,
    apply_self_pin_exemption,
    find_upstream_ref_pins,
    github_candidates,
    parse_min_age,
    select_latest,
)
from .versions import is_newer, safe_version, strip_dev_suffix

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    if sys.version_info >= (3, 11):
        from importlib.resources.abc import Traversable
    else:
        from importlib.abc import Traversable


RUNTIME_FRAGMENTS: tuple[str, ...] = (
    "release.yaml",
    "vt-trend-chart.js",
)
"""Bundled files loaded by `repomatic` at runtime, not deployed verbatim.

These files live in `repomatic/data/` so they ship in the wheel and are
discoverable via {func}`get_data_content`, but `repomatic init` never copies them
as-is. `release.yaml` is the canonical caller `repomatic.github.workflow_sync`
reads to assemble each downstream `release.yaml`, copying its jobs and rewriting
the local `uses:` refs (see `_generate_release_caller`); the deployed
`release.yaml` is generated, not this bundled copy. `vt-trend-chart.js` is the
detections-chart script `repomatic.binaries_page.render_chart_section` splices
into `docs/binaries.md` with its payload placeholders filled. New entries must
be added explicitly so the data-file registry tests stay authoritative.
"""


# Exportable files: all registry entries + tool runner bundled defaults.
EXPORTABLE_FILES: dict[str, str | None] = {
    **{f.source: f.target for c in COMPONENTS for f in c.files if not f.tree},
    **{c.source_file: None for c in COMPONENTS if isinstance(c, ToolConfigComponent)},
    # Standalone linter configs from the tool runner (yamllint, zizmor).
    # These are bundled defaults used at runtime, not init components.
    **{
        spec.default_config: None
        for spec in TOOL_REGISTRY.values()
        if spec.default_config
    },
    # Internal-use fragments loaded by the package at runtime.
    **dict.fromkeys(RUNTIME_FRAGMENTS),
}
"""Registry of all exportable files: maps filename to default output path.

`None` means the file is bundled but not directly written to a target path
by `repomatic init` (used for `pyproject.toml` templates that need merging,
tool-runner default configs, and runtime fragments).
"""


def export_content(filename: str) -> str:
    """Get the content of any exportable bundled file.

    :param filename: The filename (like "ruff.toml" or "release.yaml").
    :return: Content of the file as a string.
    :raises ValueError: If the file is not in the registry.
    :raises FileNotFoundError: If the file doesn't exist.
    """
    if filename not in EXPORTABLE_FILES:
        supported = ", ".join(EXPORTABLE_FILES.keys())
        msg = f"Unknown file: {filename!r}. Supported: {supported}"
        raise ValueError(msg)

    return get_data_content(filename)


# ---------------------------------------------------------------------------
# pyproject.toml config merging.
# ---------------------------------------------------------------------------


def _strip_header_comments(native_source: str) -> str:
    """Drop the file-level header block from a native-format template.

    Templates open with header comments explaining the standalone format
    (`bumpversion.toml` etc.); those do not belong in the `[tool.X]` section
    grafted into `pyproject.toml`. Everything below them does, per-key comments
    included: they are the only documentation a downstream `pyproject.toml`
    ever carries for a key it did not write.

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

    Skipping to the first non-comment line instead, as this did until it was
    caught by a downstream `[tool.typos]` whose identifier map arrived
    undocumented, swallows the second comment along with the header: six of the
    nine bundled templates lost their first key's documentation that way. The
    blank-line convention is pinned by `test_native_templates_separate_header`.

    ```{note}
    Use `splitlines(keepends=True)`, not `"\\n".join(splitlines(...))`: the
    template's trailing newline carries over to the final KV slot's EOL
    trivia, and a synced section that happens to land at end-of-document
    needs that newline to survive so the rendered file ends with `\\n`. The
    invariant is pinned by `test_update_tool_config_preserves_trailing_newline`.
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


# Sentinel marking a key absent from the template, so a legitimate template
# value of `None` is not mistaken for "missing" during the merge walk.
_MISSING = object()


def _entry_identity(
    entry: Mapping[str, Any], identity_keys: tuple[str, ...]
) -> tuple[tuple[str, Any], ...]:
    """Return the slot identity of an array-of-tables entry.

    The identity is the tuple of `(key, value)` pairs for the `identity_keys`
    the entry actually carries, so two entries map to the same slot when they
    agree on every identity key they share. See
    {attr}`~repomatic.registry.ToolConfigComponent.graft_identity_keys`.
    """
    return tuple((key, entry[key]) for key in identity_keys if key in entry)


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
      project-specific table like `[tool.typos.files]` survives untouched.
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

    Grafted nodes are copied from *existing*, so comments and inline
    formatting on local additions carry over.

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
            grafted = [
                item
                for item in existing_value
                # Same slot as a canonical entry: superseded by the template.
                if not (
                    isinstance(item, dict)
                    and _entry_identity(item, identity_keys) in template_slots
                )
                and item not in template_value
            ]
            if list(existing_value) == list(template_value) + grafted:
                # The merge changes nothing, so keep the array the project
                # wrote instead of rebuilding an equal one. Rebuilding re-emits
                # every item in tomlrt's own style, because iterating an array
                # yields decoded values rather than the nodes carrying their
                # source lexeme: a literal `'base32 "[0-9a-z]{52}"'` comes back
                # as an escaped `"base32 \"[0-9a-z]{52}\""`. Same content, and
                # `pyproject-fmt` rewrites it straight back, so the unattended
                # `sync-repomatic` and `format-pyproject` jobs each open a pull
                # request undoing the other's, forever. Keeping the node also
                # spares the per-entry comments a rebuild would drop.
                target[key] = existing_value
                continue
            for item in grafted:
                target[key].append(item)
        # Scalar in both: the canonical template value wins, nothing to graft.


def _overlay_owned_keys(
    doc: Any,
    existing_section: Any,
    comp: ToolConfigComponent,
    content: str,
) -> str | None:
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

    :param doc: The parsed `pyproject.toml` document, mutated in place.
    :param existing_section: The live `[tool.X]` table being overlaid.
    :param comp: The component whose owned keys are being synced.
    :param content: The current `pyproject.toml` content, for no-op detection.
    :return: Modified content, or `None` when the owned keys already match.
    """
    native_source = export_content(comp.source_file)
    template_plain = tomlrt.loads(_strip_header_comments(native_source)).to_dict()
    for key, value in template_plain.items():
        existing_section[key] = value

    modified = tomlrt.dumps(doc)
    if modified.strip() == content.strip():
        logging.info(f"[{comp.tool_section}] already up to date.")
        return None

    logging.info(f"Synced repomatic-owned keys in [{comp.tool_section}].")
    return modified


def _update_tool_config(
    doc: tomlrt.Document,
    content: str,
    comp: ToolConfigComponent,
) -> str | None:
    """Re-derive an existing `[tool.X]` section from the bundled template.

    Rebuilds the section from the canonical template, then grafts local-only
    configuration back on via {func}`_graft_local_additions`: keys the template
    omits, extra items in shared arrays, and extra keys in shared nested
    tables. The template wins on shared scalars; `comp.preserved_keys`
    overrides that for named top-level keys, like `current_version`.

    When `comp.overlay` is set the template is partial: only its own keys are
    updated in place via {func}`_overlay_owned_keys`, leaving the rest of the
    project-owned section (and its key order) intact.

    :param doc: The parsed pyproject.toml document, whose `[tool]` table the
        caller ({func}`init_config`) already found `comp.tool_name` in.
    :param content: The current pyproject.toml content, for the no-op check.
    :param comp: The component whose config is being synced.
    :return: Modified pyproject.toml content, or `None` if already up to date.
    """
    tool_table = doc["tool"]
    existing_section = tool_table[comp.tool_name]

    if comp.overlay:
        return _overlay_owned_keys(doc, existing_section, comp, content)

    # Plain-dict view of the canonical template, walked alongside the existing
    # section to decide what local-only content to graft back on.
    native_source = export_content(comp.source_file)
    template_plain = tomlrt.loads(native_source).to_dict()

    # Build the replacement section from the template, stripping file-level
    # comments that only apply to the standalone format. The template is kept
    # as a parsed `Document`, not lifted via `Table.section()`: a direct
    # `tool_table[name] = parsed_document` assignment routes through tomlrt's
    # trivia-preserving clone path (dimbleby/tomlrt#171, fixed in 1.7.6), so
    # standalone comments above scalar keys carry over with the values.
    native_stripped = _strip_header_comments(native_source)
    new_section = tomlrt.loads(native_stripped)

    # Graft local-only configuration on top of the canonical template: keys the
    # template omits, extra items in shared arrays, and extra keys in shared
    # nested tables all survive, while the template wins on shared scalars and
    # on array-of-tables entries that share a `graft_identity_keys` slot.
    # Graft before assigning: the assignment deep-clones `new_section`, so
    # grafted entries must be in place first to carry over with their comments.
    _graft_local_additions(
        new_section,
        template_plain,
        existing_section,
        comp.graft_identity_keys,
    )

    # `preserved_keys` are authoritative locally (`current_version`): the
    # local value overrides the template placeholder.
    for key in comp.preserved_keys:
        if key in existing_section:
            new_section[key] = existing_section[key]

    # Replace the section in the document.
    tool_table[comp.tool_name] = new_section

    modified = tomlrt.dumps(doc)

    if modified.strip() == content.strip():
        logging.info(f"[{comp.tool_section}] already up to date.")
        return None

    logging.info(f"Replaced [{comp.tool_section}] from bundled template.")
    return modified


def init_config(config_type: str, pyproject_path: Path | None = None) -> str | None:
    """Initialize a configuration by merging it into pyproject.toml.

    Reads the pyproject.toml file, checks if the tool section already exists,
    and if not, inserts the bundled template at the appropriate location.

    The template is stored in native format (without `[tool.X]` prefix) and
    is parsed by tomlrt and added under the `[tool]` table.

    :param config_type: The configuration type (like `"ruff"` or
        `"bumpversion"`).
    :param pyproject_path: Path to pyproject.toml. Defaults to
        `./pyproject.toml`.
    :return: The modified pyproject.toml content, or `None` if no changes
        needed.
    :raises ValueError: If the config type is not supported.
    """
    comp = COMPONENTS_BY_NAME.get(config_type)
    if not isinstance(comp, ToolConfigComponent):
        supported = ", ".join(
            c.name for c in COMPONENTS if isinstance(c, ToolConfigComponent)
        )
        msg = f"Unknown config type: {config_type!r}. Supported: {supported}"
        raise TypeError(msg)

    if pyproject_path is None:
        pyproject_path = Path("pyproject.toml")

    if not pyproject_path.exists():
        logging.error(f"File not found: {pyproject_path}")
        return None

    content = pyproject_path.read_text(encoding="UTF-8")
    doc = tomlrt.loads(content)

    # Check if the config section already exists.
    tool_table = doc.get("tool")
    if tool_table and comp.tool_name in tool_table:
        if comp.sync_mode == SyncMode.ONGOING:
            return _update_tool_config(doc, content, comp)
        logging.info(f"[{comp.tool_section}] already exists in {pyproject_path.name}")
        return None

    # Load the template and strip file-level comments. The template is kept
    # as a parsed `Document`: `doc.install` clones the document body under the
    # synthesised header via tomlrt's trivia-preserving path, so standalone
    # comments and inline-array bracket pad carry over (dimbleby/tomlrt#171).
    native_source = export_content(comp.source_file)
    native_stripped = _strip_header_comments(native_source)
    new_section = tomlrt.loads(native_stripped)

    doc.install(("tool", comp.tool_name), new_section)

    return tomlrt.dumps(doc)


# ---------------------------------------------------------------------------
# Repository initialization.
# ---------------------------------------------------------------------------


def _base_version() -> str:
    """The running repomatic version with any PEP 440 `.devN` suffix stripped.

    `"5.10.0.dev0"` becomes `"5.10.0"`. The bare (no `v`) form, used both for the
    default pin and to age-check the running version against a release
    datasource.
    """
    return strip_dev_suffix(__version__)


def default_version_pin() -> str:
    """Derive the default version pin from `__version__`.

    Strips any `.dev0` suffix and prefixes with `v`. For example,
    `"5.10.0.dev0"` becomes `"v5.10.0"`.
    """
    return f"v{_base_version()}"


def _select_cooldown_pin(
    candidates: list[Candidate],
    base: str,
    min_age: timedelta,
    today: date,
) -> Candidate | None:
    """Pick the release `init` should pin when the cooldown holds `base` back.

    `init` normally pins the running repomatic version (`base`). When that
    version is still inside the `minimum-release-age` window (a fresh release a
    `--refresh` just pulled), pinning it would adopt an unproven cut, bypassing
    the cooldown every other version adopter honors. This returns the newest
    release *older* than `base` that has cleared the window, so the downstream
    pin steps back to a proven release instead.

    The step-back is skipped (returns `None`, meaning "keep pinning `base`")
    whenever `base` is safe or nothing better exists:

    - `base` is absent from *candidates* (an unreleased `.dev` cut, or the
      datasource is unavailable): fail open to `base`.
    - `base` has already cleared the cooldown: the common case, no change.
    - No cooldown-cleared release is strictly older than `base`: nothing to step
      back to.

    :param candidates: Releases offered by the datasource.
    :param base: The running repomatic version (bare, no `v` prefix).
    :param min_age: The `minimum-release-age` stabilization window.
    :param today: Reference date for the cooldown computation.
    :return: The {class}`~repomatic.version_sync.Candidate` to pin instead of
        `base`, or `None` to keep pinning `base`.
    """
    cutoff = today - min_age
    base_date = next((c.date for c in candidates if c.version == base), None)
    if base_date is None:
        return None
    try:
        released = date.fromisoformat(base_date)
    except ValueError:
        return None
    # `base` is old enough: the overwhelmingly common case, pin it unchanged.
    if released <= cutoff:
        return None
    latest = select_latest(candidates, min_age, today)
    if latest is None or not is_newer(base, latest.version):
        return None
    return latest


def _note_cooldown(warnings: list[str] | None, note: str) -> None:
    """Log a cooldown decision and surface it in the `init` summary."""
    if warnings is not None:
        warnings.append(note)
    logging.warning(note)


def _highest_upstream_pin(output_dir: Path, repo: str) -> UpstreamRefPin | None:
    """The highest upstream `uses:` pin already committed under *output_dir*.

    Scans {data}`~repomatic.registry.GITHUB_YAML_PATTERNS` resolved against
    *output_dir*, the same files `sync-workflow-pins` bumps, and returns the
    winning {class}`~repomatic.version_sync.UpstreamRefPin`. Stragglers left
    behind at an older pin therefore converge upward onto the repository-wide
    maximum, matching how `sync-action-pins` treats a slug pinned at more than
    one version.

    Only *repo* is matched, not every slug in
    {data}`~repomatic.registry.UPSTREAM_REPO_SLUGS`: a caller still naming a
    pre-rename slug is a tombstone `init` rewrites, not a pin to preserve.

    :param output_dir: Root of the target repository.
    :param repo: Upstream `owner/repo` whose refs are read.
    :return: The highest pin found, or `None` for a repository carrying none.
    """
    best: UpstreamRefPin | None = None
    for pattern in GITHUB_YAML_PATTERNS:
        for path in sorted(output_dir.glob(pattern)):
            try:
                content = path.read_text(encoding="UTF-8")
            except OSError:
                continue
            for pin in find_upstream_ref_pins(content, repo):
                if best is None:
                    best = pin
                    continue
                # At equal versions a SHA-pinned ref beats a bare tag one, so
                # the floor never unpins a hardened ref.
                hardens = pin.version == best.version and best.sha is None
                if hardens or is_newer(pin.version, best.version):
                    best = pin
    return best


def resolve_default_pin(
    config: Config,
    *,
    repo: str = DEFAULT_REPO,
    today: date | None = None,
    warnings: list[str] | None = None,
    floor: UpstreamRefPin | None = None,
) -> tuple[str, str | None]:
    """Resolve the upstream pin, holding a fresh release back by cooldown.

    Returns the `(version, commit_sha)` `init` stamps into thin-caller `uses:`
    refs. In the common case, and on any datasource failure, this is the running
    repomatic version paired with its build-time SHA. Only when adopting a
    release still inside the `[tool.repomatic] minimum-release-age` window does
    the pin step back to the newest cooldown-cleared release (see
    {func}`_select_cooldown_pin`), resolving that tag's SHA afresh.

    ```{important}
    The cooldown may hold back an *adoption*. It may never rewrite a pin the
    repository already carries, which is what *floor* records.

    `init` is the only writer of these refs: `sync-action-pins` skips every slug
    in {data}`~repomatic.registry.UPSTREAM_REPO_SLUGS`, and
    {data}`~repomatic.version_sync.ACTION_PIN_RE` does not even match a
    subpath-carrying reusable-workflow ref. So a downstream repository adopts a
    new repomatic release exactly one way: a human moves the pin, by hand or by
    running a newer `init`. Two things follow.

    A pin equal to the running version is not a decision to gate. The CI
    `sync-repomatic` job runs `init` at the pinned version itself, so `base`
    equals *floor* on every sync; re-judging it there downgrades the repository
    once a week after each hand-bump, and fights the only upgrade path there is.

    A pin below the running version is a skew. `init` renders caller *content*
    from the running version, so a ref naming an older release ships that
    content against an older reusable-workflow surface, which GitHub rejects as
    soon as the two disagree (see
    `test_thin_caller_workflow_call_inputs_stay_minimal`). Returning such a pin
    is therefore only half a decision: {func}`run_init` reads it back and, when
    the repository already carries workflows, skips regenerating them so the
    tree stays coherent at the pin it keeps. A first-time adoption has no tree
    to keep, so there the skew stands as the only alternative to writing no
    workflows at all.
    ```

    :param config: Repomatic config supplying the `minimum-release-age` window.
    :param repo: Upstream `owner/repo` whose releases gate the pin.
    :param today: Reference date for the cooldown; defaults to the current UTC
        date.
    :param warnings: When provided, a cooldown note is appended here (in
        addition to being logged), so `run_init` can surface it in the final
        `init` summary rather than only mid-run.
    :param floor: The highest upstream pin already committed downstream, from
        {func}`_highest_upstream_pin`. `None` for a repository carrying none,
        the one case the cooldown may step back freely.
    :return: `(version_pin, commit_sha)`. `commit_sha` is `None` when no SHA can
        be resolved, leaving a bare tag pin.
    """
    base = _base_version()
    build_sha = __git_tag_sha__ or None
    # Unreleased dev cuts never appear in a release datasource: pin as-is,
    # sparing the source repo's own `--from .` runs a pointless lookup.
    if base != __version__:
        return f"v{base}", build_sha
    if floor is not None:
        # Regeneration, not adoption: the repository already pins this exact
        # version. Answered without a datasource round-trip, which is what
        # every CI sync does. Falls back to the on-disk SHA so a build carrying
        # no tag SHA of its own never unpins a hardened ref.
        if base == floor.version:
            return f"v{base}", build_sha or floor.sha
        # A deliberate rollback (`uvx repomatic==<older> init`). Honor it: pin
        # and content stay coherent, and holding the newer floor here would
        # make the rollback silently fail.
        if is_newer(floor.version, base):
            return f"v{base}", build_sha
    min_age = parse_min_age(config.minimum_release_age)
    if not min_age:
        return f"v{base}", build_sha
    if today is None:
        today = datetime.now(timezone.utc).date()
    repo_url = f"https://github.com/{repo}"
    stepped_back = _select_cooldown_pin(
        github_candidates(repo_url), base, min_age, today
    )
    if stepped_back is None:
        return f"v{base}", build_sha
    # The cleared release is no better than what the repository already runs:
    # decline the adoption instead of regressing to it.
    if floor is not None and not is_newer(stepped_back.version, floor.version):
        _note_cooldown(
            warnings,
            f"repomatic {base} is inside the {config.minimum_release_age} "
            f"minimum-release-age window; keeping the release this repository "
            f"already pins, v{floor.version}. Pass --no-cooldown to adopt "
            f"{base} now.",
        )
        return f"v{floor.version}", floor.sha
    _note_cooldown(
        warnings,
        f"repomatic {base} is inside the {config.minimum_release_age} "
        f"minimum-release-age window; pinning the newest cleared release "
        f"v{stepped_back.version} instead. Pass --no-cooldown to pin {base}.",
    )
    # Fall back to a bare tag pin when the SHA lookup fails.
    return f"v{stepped_back.version}", resolve_tag_to_sha(repo_url, stepped_back.ref)


@dataclass
class InitResult:
    """Result of a repository initialization run."""

    created: list[str] = field(default_factory=list)
    """Relative paths of newly created files."""

    updated: list[str] = field(default_factory=list)
    """Relative paths of existing files overwritten with new content."""

    skipped: list[str] = field(default_factory=list)
    """Relative paths of skipped (already existing) files."""

    excluded: list[str] = field(default_factory=list)
    """Exclude entries that were applied."""

    excluded_existing: list[str] = field(default_factory=list)
    """Relative paths of excluded files that still exist on disk."""

    unmodified_configs: list[str] = field(default_factory=list)
    """Relative paths of config files identical to bundled defaults."""

    removed_prunable: list[tuple[str, str]] = field(default_factory=list)
    """`(relative_path, successor)` for on-disk orphans of dropped assets
    whose content matches the last-shipped version (safe to auto-delete)."""

    removed_review: list[tuple[str, str]] = field(default_factory=list)
    """`(relative_path, successor)` for on-disk orphans of dropped assets that
    differ from the last-shipped version (locally modified: reported for
    manual review, never auto-deleted)."""

    warnings: list[str] = field(default_factory=list)
    """Warning messages emitted during initialization."""


def _relative_label(target: Path, output_dir: Path | None) -> str:
    """Render *target* for a log line, relative to the repository root.

    Every `init` log line names a repository-relative POSIX path, so the output
    of one run reads uniformly whatever wrote the file. Falls back to the path
    as given when no root is known, or when *target* sits outside it.
    """
    if output_dir is None:
        return str(target)
    try:
        return target.relative_to(output_dir).as_posix()
    except ValueError:
        return str(target)


def _write_managed(
    target: Path,
    content: str,
    result: InitResult,
    output_dir: Path,
    *,
    normalize: bool = True,
    verb: str = "",
) -> str:
    """Write *content* to *target* and classify the outcome on *result*.

    The single writer behind every text file `init` materializes. Each caller
    used to repeat the same five steps (compare against what is on disk, create
    the parent directory, write, pick the `created` or `updated` bucket, log),
    and they had drifted: only one normalized trailing whitespace, and only one
    logged an absolute path.

    Normalization matters beyond cosmetics. {func}`_detect_removed_assets` and
    {func}`tool_runner.find_unmodified_configs` both hash the `rstrip() +
    "\\n"` form and compare it against what `init` writes, so a caller that
    skipped the normalization would put a file on disk those two could never
    recognize. Both sides of the comparison are normalized, so a file
    differing only in trailing whitespace still counts as unchanged and is
    left alone.

    :param target: Absolute path to write.
    :param content: The new text.
    :param result: {class}`InitResult` accumulator, mutated in place.
    :param output_dir: Repository root, for the reported relative path.
    :param normalize: Collapse trailing whitespace to a single newline. Off for
        content whose exact bytes matter (a generated workflow).
    :param verb: Overrides the log line's leading word (`Synced header`).
    :return: `"created"`, `"updated"`, or `"unchanged"`.
    """
    text = content.rstrip() + "\n" if normalize else content
    rel = _relative_label(target, output_dir)
    existed = target.exists()
    if existed:
        current = target.read_text(encoding="UTF-8")
        if (current.rstrip() + "\n" if normalize else current) == text:
            logging.debug(f"Unchanged: {rel}")
            return "unchanged"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="UTF-8")
    if existed:
        result.updated.append(rel)
        logging.info(f"{verb or 'Updated'}: {rel}")
        return "updated"
    result.created.append(rel)
    logging.info(f"{verb or 'Created'}: {rel}")
    return "created"


def _unlink_with_empty_parents(target: Path, root: Path) -> None:
    """Delete `target`, then prune now-empty parent directories up to `root`.

    A skill is a whole folder, so *target* may be a directory: remove it and
    everything it carries (`scripts/`, `references/`, `assets/`) in one go.
    """
    if target.is_dir():
        rmtree(target)
    else:
        target.unlink()
    parent = target.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def prune_paths(
    paths: Sequence[str] | Sequence[tuple[str, str]],
    output_dir: Path,
    *,
    prune_parents: bool = True,
) -> None:
    """Delete every path of an {class}`InitResult` report section.

    The deleting half of `init`'s delete flags (`--delete-excluded`,
    `--delete-unmodified`, the removed-asset pruning), kept beside
    {func}`run_init`, which produced the paths: the CLI decides *which*
    sections get deleted, this module owns the filesystem mutation.

    :param paths: Bare relative paths, or `(path, successor)` pairs for the
        removed-asset sections.
    :param output_dir: Repository root the paths are relative to.
    :param prune_parents: Also remove parent directories left empty. On for the
        removed-asset and excluded sections, whose targets sit in directories
        repomatic itself created (`.claude/skills/<name>/`). Off for unmodified
        tool configs, which share `.github/` and the repository root with files
        repomatic does not own.
    """
    for entry in paths:
        path = entry[0] if isinstance(entry, tuple) else entry
        target = output_dir / path
        if prune_parents:
            _unlink_with_empty_parents(target, output_dir)
        else:
            target.unlink()


def adopted_ongoing_configs(output_dir: Path) -> set[str]:
    """Return the ongoing tool configs whose section `pyproject.toml` already carries.

    {attr}`~repomatic.registry.InitDefault.EXPLICIT` governs *adoption*, not
    upkeep: it keeps a bare `init` from pushing `[tool.typos]` onto a repository
    that never asked for one. Once the section is there the repository has
    asked, so an {attr}`~repomatic.registry.SyncMode.ONGOING` component rejoins
    the bare-init set and resumes tracking the bundled template.

    Without this the two flags cancel out. The only sync that ever runs
    unattended is the bare `init` the `sync-repomatic` job calls, so an ONGOING
    section is otherwise re-derived only when a human types its component name,
    and a `[tool.typos]` written by hand sits indefinitely beside a bundled
    template it never adopts a single rule from.

    {attr}`~repomatic.registry.SyncMode.BOOTSTRAP` components stay out: their
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
        for comp in COMPONENTS
        if isinstance(comp, ToolConfigComponent)
        and comp.init_default is InitDefault.EXPLICIT
        and comp.sync_mode is SyncMode.ONGOING
        and comp.tool_name in tool_table
    }


def run_init(
    output_dir: Path,
    components: Sequence[str] = (),
    version: str | None = None,
    cooldown: bool = True,
    repo: str = DEFAULT_REPO,
    repo_slug: str | None = None,
    config: Config | None = None,
) -> InitResult:
    """Bootstrap a repository for use with `kdeldycke/repomatic`.

    Creates thin-caller workflow files, exports configuration files, and
    generates a minimal `changelog.md` if missing. Managed files (workflows,
    configs, skills) are always overwritten. User-owned files
    (`changelog.md`, `zizmor.yaml`) are created once and never overwritten.

    For `awesome-*` repositories, the `awesome-template` component is
    auto-included when no explicit component selection is made.

    ```{note}
    Scope exclusions (`RepoScope.AWESOME_ONLY`, `PYTHON_ONLY`) and
    user-config exclusions (`[tool.repomatic] exclude`) only apply
    during bare `repomatic init`. When components are explicitly named
    on the CLI, scope is bypassed: the caller knows what they asked for.
    This allows workflows to materialize out-of-scope configs at runtime
    (like `repomatic init publish-pypi-action` in a non-Python repo).
    ```

    :param output_dir: Root directory of the target repository.
    :param components: Components to initialize. Empty means all defaults.
        When non-empty, scope and user-config exclusions are bypassed.
    :param version: Version pin for upstream workflows (like `v5.10.0`). When
        `None`, derived from the running package version, gated by *cooldown*.
    :param cooldown: When `True` (and *version* is unset), hold the derived pin
        back to the newest release past the `[tool.repomatic]
        minimum-release-age` window instead of pinning a fresh running version
        (see {func}`resolve_default_pin`). Ignored when *version* is explicit.
    :param repo: Upstream repository containing reusable workflows.
    :param repo_slug: Repository `owner/name` slug for awesome-template URL
        rewriting. Auto-detected via {class}`Metadata` if not provided.
    :param config: The resolved `[tool.repomatic]` configuration. Loaded from
        the current directory when omitted, so a caller working against another
        tree must pass the config it read from there.
    :return: Summary of created, updated, skipped, and warned items.
    """
    # Parse CLI selection. Entries may be bare component names ("skills")
    # or qualified component/file selectors ("skills/repomatic-topics").
    if components:
        selected_full, selected_files = parse_component_entries(
            list(components), context="selection"
        )
        # Bare component name overrides file-level selection for the same
        # component — "skills" means all skills even if "skills/x" also
        # appears.
        for name in selected_full:
            selected_files.pop(name, None)
        selected = selected_full | set(selected_files.keys())
    else:
        selected_full = set()
        selected_files = {}
        selected = {
            c.name
            for c in COMPONENTS
            if c.init_default in (InitDefault.INCLUDE, InitDefault.EXCLUDE)
        }
        selected |= adopted_ongoing_configs(output_dir)
    result = InitResult()

    # Auto-include awesome-template for awesome-* repositories.
    if not repo_slug:
        repo_slug = Metadata().repo_slug
    is_awesome = bool(repo_slug and is_awesome_repo(repo_slug.split("/")[-1]))
    is_python = is_python_project(output_dir)
    is_package = is_python_package(output_dir)
    logging.debug(
        f"Repository traits: awesome={is_awesome} python={is_python} "
        f"package={is_package}"
    )
    if is_awesome and not components:
        selected.add("awesome-template")

    # Load config for source path resolution and exclusion rules.
    if config is None:
        config = load_repomatic_config()
    source_paths = resolve_source_paths(config)

    # Parse exclude/include config. User exclude is additive to defaults;
    # user include overrides both. Qualified entries (component/file)
    # implicitly select the parent component.
    user_exclude: list[str] = config.exclude
    user_include: list[str] = config.include
    if user_include:
        include_full, include_files = parse_component_entries(
            user_include, context="include"
        )
    else:
        include_full, include_files = set(), {}
    default_exclusions = {
        c.name for c in COMPONENTS if c.init_default == InitDefault.EXCLUDE
    }
    # Ephemeral components stay out of the working tree on a bare init, and
    # `include` cannot override that: their files are regenerated by whatever
    # reads them, so a committed copy is never the one that gets used. Union
    # them in last, after `include` has had its say on everything else.
    ephemeral = {c.name for c in COMPONENTS if c.ephemeral}
    if not components:
        for name in sorted(ephemeral & (set(user_include) | set(include_files))):
            logging.warning(
                f"`include` lists the ephemeral component {name!r}, which is "
                "ignored: its files are regenerated on demand by the commands "
                f"that read them. Run `repomatic init {name}` to write them out."
            )
    exclude_entries = sorted(
        (
            (default_exclusions | set(user_exclude))
            - set(user_include)
            - set(include_files)
        )
        | ephemeral
    )
    if default_exclusions:
        logging.debug(f"Default exclusions: {', '.join(sorted(default_exclusions))}")
    if user_exclude:
        logging.debug(f"User exclude: {', '.join(user_exclude)}")
    if user_include:
        logging.debug(f"User include: {', '.join(user_include)}")
    excluded_components, excluded_files = parse_component_entries(
        exclude_entries, context="exclude"
    )

    # Apply user-configured exclusions when no explicit components given.
    if not components:
        actually_excluded = excluded_components & selected
        selected -= excluded_components
        result.excluded = sorted(
            list(actually_excluded)
            + [
                f"{c}/{f}"
                for c, fs in sorted(excluded_files.items())
                if c not in actually_excluded
                for f in sorted(fs)
            ]
        )

        # Expand component-level exclusions into file-level entries so
        # detection below is a single unified pass.
        for excl_name in actually_excluded:
            excl_comp = COMPONENTS_BY_NAME.get(excl_name)
            if excl_comp and excl_comp.files:
                ids = {e.file_id for e in excl_comp.files}
                excluded_files.setdefault(excl_name, set()).update(ids)

    # Classification pass: determine which components and files to
    # initialize, and which to flag for stale-file detection.
    #
    # Three exclusion mechanisms, applied in order per component:
    #
    # 1. Scope (component-level and file-level `RepoScope`).
    #    Bypassed by explicit CLI naming or `[tool.repomatic] include`.
    #    Scope exclusions on `selected` apply in all repos including the
    #    source repo (an AWESOME_ONLY config should not be merged into the
    #    Python source repo's `pyproject.toml`). Stale-file detection
    #    is suppressed in the source repo so bundled data files are never
    #    flagged for deletion.
    #
    # 2. Config key (component-level and file-level `config_key`).
    #    Always applies, even with explicit CLI naming: the user's
    #    `[tool.repomatic]` config is authoritative for feature flags.
    #
    # 3. User config (`[tool.repomatic] exclude`/`include`).
    #    Already applied above, before this loop.
    is_source = is_source_repo(output_dir)
    scope_excluded_targets: list[str] = []

    for reg_comp in COMPONENTS:
        # In the source repo, clear any user-config exclusions for bundled
        # components so their data files are never flagged for deletion.
        if is_source and reg_comp.files:
            excluded_files.pop(reg_comp.name, None)

        # Scope is bypassed by explicit CLI naming or config include.
        scope_bypassed = bool(components) or reg_comp.name in include_full

        # --- Component-level scope ---
        if not reg_comp.scope.matches(is_awesome, is_python, is_package):
            logging.debug(
                f"Scope exclusion: {reg_comp.name} ({reg_comp.scope.name}) not "
                f"applicable to repo (awesome={is_awesome}, python={is_python}, "
                f"package={is_package})."
            )
            if not scope_bypassed and reg_comp.name not in include_files:
                selected.discard(reg_comp.name)
                if not is_source:
                    if isinstance(reg_comp, GeneratedComponent) and reg_comp.target:
                        scope_excluded_targets.append(reg_comp.target)
                    elif reg_comp.files:
                        ids = {e.file_id for e in reg_comp.files}
                        excluded_files.setdefault(reg_comp.name, set()).update(ids)
                continue

        # --- Component-level config_key ---
        if reg_comp.name in selected and not reg_comp.is_enabled(config):
            selected.discard(reg_comp.name)
            logging.info(
                f"[tool.repomatic] {reg_comp.config_key} is disabled. Skipping "
                f"{reg_comp.name}."
            )

        # --- File-level scope and config_key ---
        for entry in reg_comp.files:
            if not entry.scope.matches(is_awesome, is_python, is_package):
                logging.debug(
                    f"Scope exclusion: {reg_comp.name}/{entry.file_id} "
                    f"({entry.scope.name}) not applicable to repo "
                    f"(awesome={is_awesome}, python={is_python}, package={is_package})."
                )
                if (
                    not scope_bypassed
                    and not is_source
                    and entry.file_id not in include_files.get(reg_comp.name, set())
                ):
                    excluded_files.setdefault(reg_comp.name, set()).add(entry.file_id)
            if not is_source and not entry.is_enabled(config):
                logging.debug(
                    f"Config exclusion: {reg_comp.name}/{entry.file_id} "
                    f"({entry.config_key} disabled)."
                )
                excluded_files.setdefault(reg_comp.name, set()).add(entry.file_id)

    # Detect excluded files that still exist on disk.
    for comp_name, file_ids in sorted(excluded_files.items()):
        for fid in sorted(file_ids):
            rel = excluded_rel_path(comp_name, fid)
            if rel:
                rel = _resolve_target(comp_name, rel, config)
                if (output_dir / rel).exists():
                    result.excluded_existing.append(rel)
    for rel in sorted(scope_excluded_targets):
        if (output_dir / rel).exists():
            result.excluded_existing.append(rel)

    # Detect orphans of assets repomatic has dropped (renamed or removed
    # upstream). Suppressed in the source repo, where the data files for
    # these tombstones no longer exist anyway.
    if not is_source:
        result.removed_prunable, result.removed_review = _detect_removed_assets(
            output_dir, config
        )

    # Dispatch by component type.
    tool_configs_to_merge: list[str] = []

    logging.debug(f"Selected components: {', '.join(sorted(selected))}")

    # Resolve the upstream version pin (and its SHA) only when a workflow is
    # actually generated: config-only inits (labels, bumpversion) need no pin
    # and must not pay for the cooldown datasource lookup. An explicit --version
    # keeps its build-time SHA and bypasses the cooldown. The pin already on
    # disk is the floor: the cooldown gates an adoption, never a regeneration.
    commit_sha: str | None = __git_tag_sha__ or None
    if version is None:
        workflows_selected = any(
            isinstance(COMPONENTS_BY_NAME.get(name), WorkflowComponent)
            for name in selected
        )
        if cooldown and workflows_selected:
            floor = _highest_upstream_pin(output_dir, repo)
            version, commit_sha = resolve_default_pin(
                config,
                repo=repo,
                warnings=result.warnings,
                floor=floor,
            )
            # A pin the cooldown held below the running version cannot carry
            # this version's caller content. `init` renders bodies from the
            # running wheel and substitutes only the ref, so writing them
            # beside an older `uses:` pin ships half an adoption: the trigger
            # blocks, `concurrency` groups and `env:` of the new release,
            # against the reusable-workflow surface of the old one. Declining
            # the pin has to decline the content with it, or the cooldown buys
            # nothing while the caller half lands anyway, silently.
            #
            # Gated on *floor*, because only a repository that already carries
            # workflows has somewhere to stand: leaving them untouched keeps a
            # coherent tree at *floor*. A first-time adoption has no such
            # fallback, since skipping would write no workflows at all, and is
            # the one case `resolve_default_pin` documents the skew as
            # unavoidable.
            if floor is not None and is_newer(
                _base_version(), version.removeprefix("v")
            ):
                _note_cooldown(
                    result.warnings,
                    f"Leaving the workflows at {version}: their content belongs "
                    f"to repomatic {_base_version()}, which the pin above "
                    "declines. Pass --no-cooldown to adopt both together.",
                )
                selected -= {
                    name
                    for name in selected
                    if isinstance(COMPONENTS_BY_NAME.get(name), WorkflowComponent)
                }
        else:
            version = default_version_pin()

    for comp in COMPONENTS:
        if comp.name not in selected:
            continue

        file_exclude = frozenset(excluded_files.get(comp.name, set()))
        file_include = (
            frozenset(selected_files[comp.name])
            if comp.name in selected_files
            else None
        )

        if isinstance(comp, WorkflowComponent):
            _init_workflows(
                output_dir,
                repo,
                version,
                result,
                commit_sha=commit_sha,
                exclude=file_exclude,
                include=file_include,
                source_paths=source_paths,
                config=config,
            )

        elif isinstance(comp, BundledComponent):
            _init_config_files(
                output_dir,
                comp.name,
                result,
                exclude_ids=file_exclude,
                include_ids=file_include,
                config=config,
            )
            # Labels have extra files fetched from [tool.repomatic] config.
            if comp.name == "labels":
                _fetch_extra_labels(output_dir, result, config=config)

        elif isinstance(comp, TemplateComponent):
            if repo_slug:
                init_awesome_template(output_dir, repo_slug, result)

        elif isinstance(comp, GeneratedComponent):
            # Each generated component has its own producer, so dispatch by name:
            # the class alone no longer identifies one.
            if comp.name == "changelog":
                _init_changelog(output_dir, result, config=config)
            elif comp.name == "plugin":
                _init_plugin_settings(output_dir, result, config=config)

        elif isinstance(comp, ToolConfigComponent):
            tool_configs_to_merge.append(comp.name)

    # Merge tool configs into pyproject.toml (batched for efficiency).
    if tool_configs_to_merge:
        _init_tool_configs(output_dir, tool_configs_to_merge, result)

    # Check for native tool config files identical to bundled defaults.
    # Init-managed files (like labels) are already handled inline by
    # _init_config_files, so only check tool_runner configs here. The scan
    # roots at output_dir so `--delete-unmodified` (which joins these paths
    # against output_dir) deletes from the tree it scanned.
    for _tool_name, rel_path in find_unmodified_configs(output_dir):
        result.unmodified_configs.append(rel_path)
        logging.warning(f"Unmodified config (matches bundled default): {rel_path}")

    return result


def _init_workflows(
    output_dir: Path,
    repo: str,
    version: str,
    result: InitResult,
    *,
    commit_sha: str | None = None,
    exclude: frozenset[str] = frozenset(),
    include: frozenset[str] | None = None,
    source_paths: list[str] | None = None,
    config: Config | None = None,
) -> None:
    """Generate thin-caller workflows and sync non-reusable workflow headers.

    :param commit_sha: SHA to pin the `uses:` refs to, paired with *version* as
        `@sha # version`. Used verbatim: `None` yields a bare `@version` tag pin
        the next `sync-action-pins` run re-hardens. The caller
        ({func}`run_init`) owns the resolution so a cooldown step-back can pair
        the stepped-back tag with its own SHA.
    :param include: When not `None`, only generate files in this set.
    """
    workflows = REUSABLE_WORKFLOWS
    if include is not None:
        workflows = tuple(w for w in workflows if w in include)
    if exclude:
        workflows = tuple(w for w in workflows if w not in exclude)

    if config is None:
        config = load_repomatic_config()

    # Respect the workflow-sync opt-out: skip thin callers and headers,
    # leaving any existing workflow files untouched (matches the sibling
    # `*.sync` toggles). See {class}`~repomatic.config.WorkflowConfig`.
    if not config.workflow.sync:
        logging.info(
            "[tool.repomatic] workflow.sync is disabled. Skipping workflow sync."
        )
        return

    # Exclude config-gated workflows whose toggle is off.
    for entry in COMPONENTS_BY_NAME["workflows"].files:
        if entry.file_id in workflows and not entry.is_enabled(config):
            workflows = tuple(w for w in workflows if w != entry.file_id)

    paths_spec = PathsSpec(
        source_paths=source_paths,
        extra_paths=list(config.workflow.extra_paths),
        ignore_paths=list(config.workflow.ignore_paths),
        workflow_paths={k: list(v) for k, v in config.workflow.paths.items()},
    )

    workflows_dir = output_dir / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    # Generate thin-caller workflows for reusable workflows.
    for filename in workflows:
        target = workflows_dir / filename
        content, _existing_content = render_thin_caller_for_target(
            filename,
            target,
            repo=repo,
            version=version,
            paths_spec=paths_spec,
            commit_sha=commit_sha,
        )
        # A generated workflow is written byte-for-byte: its trailing layout is
        # the generator's business, not something to normalize here.
        _write_managed(target, content, result, output_dir, normalize=False)

    # Sync headers for non-reusable workflows that already exist on disk.
    for filename in sorted(NON_REUSABLE_WORKFLOWS):
        if include is not None and filename not in include:
            continue
        if exclude and filename in exclude:
            continue
        target = workflows_dir / filename
        if not target.exists():
            continue
        try:
            canonical_header = generate_workflow_header(filename, paths_spec=paths_spec)
        except (ValueError, FileNotFoundError):
            logging.warning(f"Cannot extract header for {filename}. Skipping.")
            continue
        existing = target.read_text(encoding="UTF-8")
        jobs_match = re.search(r"^jobs:", existing, re.MULTILINE)
        if jobs_match is None:
            continue
        content = canonical_header + existing[jobs_match.start() :]
        # The body below `jobs:` is the downstream repository's own, so only the
        # header is replaced and the file is written exactly as assembled.
        _write_managed(
            target,
            content,
            result,
            output_dir,
            normalize=False,
            verb="Synced header",
        )

    _realign_inline_pins(workflows_dir, version, result, output_dir, repo=repo)
    # Runs after the realignment, so it reads the pin the workflows now carry.
    _check_metadata_keys(workflows_dir, result, output_dir, version, repo=repo)


def _realign_inline_pins(
    workflows_dir: Path,
    version: str,
    result: InitResult,
    output_dir: Path,
    *,
    repo: str = DEFAULT_REPO,
) -> None:
    """Realign inline `<package>==X.Y.Z` literals onto the pin just written.

    A workflow that reaches the upstream toolkit from a `run:` shell command
    (`uvx 'repomatic==1.2.3' metadata`) must keep that version in lockstep with
    the SHA-pinned `uses:` refs above it, which
    {func}`~repomatic.lint_repo.check_inline_pins_match_upstream` enforces.
    `init` writes those refs and is the only writer of them, so it is also the
    only place that knows the pin changed: leaving the literal to the next
    `sync-workflow-pins` run opened a window, between the two, where the lint it
    is checked by was already red. Closing it here removes the window rather
    than reporting on it.

    Rewrites in either direction, like {func}`~repomatic.sync_ops._resolve_workflow_pins`
    does: the refs are the source of truth, and a literal ahead of them is drift
    the same way one behind them is.

    Scope is every workflow file, not only the ones `init` generated. A
    downstream repository carries the literal inside job bodies it owns (the
    `pr-body` calls of a release workflow), which header-only sync never
    touches; that is exactly where the lag hides.

    Also splices in the per-package cooldown escape hatch
    ({data}`~repomatic.prepare_release.SELF_PIN_COOLDOWN_EXEMPTION`) wherever a
    `uvx` command pinning the package lacks it. Every workflow exports a
    `UV_EXCLUDE_NEWER` covering all resolution, and the pin this function writes
    routinely names a release younger than that window, so realigning it without
    the exemption produces a command that cannot resolve at all. `--no-cooldown`
    makes that the normal case rather than the rare one: it adopts the running
    version the day it is published.

    ```{note}
    The exemption's own single-`=` literal (`repomatic=P0D`) is untouched by the
    version rewrite: it names a package, not a version. Splicing is idempotent,
    so a command already carrying it is left byte-identical.
    ```

    :param workflows_dir: The repository's `.github/workflows/` directory.
    :param version: Version just pinned into the `uses:` refs, in its `vX.Y.Z`
        tag spelling.
    :param result: {class}`InitResult` accumulator, mutated in place.
    :param output_dir: Repository root, for the reported relative path.
    :param repo: Upstream `owner/repo`; its name is the inline package to match.
    """
    package = package_of(repo)
    pin_re = re.compile(rf"\b{re.escape(package)}==[0-9]+(?:\.[0-9]+)*")
    # A `uses:` ref names a git tag and keeps the `v`; a requirement specifier
    # names the PyPI package and must not carry it. Same number, two namespaces.
    replacement = f"{package}=={version.removeprefix('v')}"

    for target in sorted(workflows_dir.glob("*.y*ml")):
        content = target.read_text(encoding="UTF-8")
        new_content = apply_self_pin_exemption(
            pin_re.sub(replacement, content), package, SELF_PIN_COOLDOWN_EXEMPTION
        )
        if new_content == content:
            continue
        _write_managed(
            target,
            new_content,
            result,
            output_dir,
            normalize=False,
            verb=f"Realigned inline {package} pin",
        )


def _run_commands(workflow: Path) -> list[str]:
    """Collect every `run:` script in a workflow file.

    :param workflow: Path to a workflow YAML file.
    :return: One entry per step carrying a `run:`. Empty when the file does not
        parse as a workflow, which is not this function's problem to report.
    """
    try:
        data = yaml.safe_load(workflow.read_text(encoding="UTF-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    commands = []
    for job in data.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                commands.append(step["run"])
    return commands


def _check_metadata_keys(
    workflows_dir: Path,
    result: InitResult,
    output_dir: Path,
    version: str,
    *,
    repo: str = DEFAULT_REPO,
) -> None:
    """Warn when a workflow asks `metadata` for a key this version dropped.

    A workflow reaches the toolkit's metadata through a `run:` command naming
    the keys it wants, and an unknown key is a hard `UsageError`. So retiring a
    key breaks every downstream workflow still asking for it, at the first push
    after the adoption, in the job every other job hangs off: click-extra's
    whole test workflow went dark that way when `coverage_cells` left with the
    Codecov integration.

    `init` is where the version changes, and the job bodies holding these
    commands belong to the downstream repository, so header-only sync never
    reads them. That makes this the one moment the mismatch is both introduced
    and fixable, ahead of the commit rather than after the red run.

    Scope is every workflow file, matching {func}`_realign_inline_pins`: the
    invocation hides in a job body wherever the repository chose to put it.

    ```{note}
    The key set is the running version's, read in-process, so the check applies
    only when the workflows end up pinned to that same version. A cooldown
    holding the pin back leaves the answer unknowable from here: the older
    release's key set is not importable, and judging it by this one's would
    blame a workflow that works.
    ```

    :param workflows_dir: The repository's `.github/workflows/` directory.
    :param result: {class}`InitResult` accumulator, mutated in place.
    :param output_dir: Repository root, for the reported relative path.
    :param version: Version just pinned into the workflows, `vX.Y.Z` spelling.
    :param repo: Upstream `owner/repo`; its name is the package to match.
    """
    # Compared on the release tuple, so a development build checks the release
    # it is heading for: the pin drops the `.devN` suffix the running version
    # carries, and string equality would skip the check on every dev checkout.
    pinned = safe_version(version.removeprefix("v"))
    running = safe_version(__version__)
    same_release = (
        pinned is not None and running is not None and pinned.release == running.release
    )
    if not same_release:
        logging.debug(
            f"Workflows pin {version}, not the running {__version__}: skipping the "
            "metadata-key check, whose key set is this version's."
        )
        return

    package = package_of(repo)
    valid_keys = all_metadata_keys()
    for target in sorted(workflows_dir.glob("*.y*ml")):
        unknown = sorted({
            key
            for command in _run_commands(target)
            for key in requested_metadata_keys(command, package)
            if key not in valid_keys
        })
        if not unknown:
            continue
        result.warnings.append(
            f"{_relative_label(target, output_dir)} asks `{package} metadata` "
            f"for {', '.join(repr(key) for key in unknown)}, which "
            f"{package} {__version__} does not emit. The job will fail; drop "
            "the key or pin an older version."
        )
        logging.warning(result.warnings[-1])


def is_source_repo(output_dir: Path) -> bool:
    """Detect whether `output_dir` is the repomatic source repository root.

    Returns `True` when `output_dir` contains the `repomatic` Python
    package source tree (`repomatic/__init__.py` and `repomatic/data/`).
    Only the upstream source repo has these. This prevents auto-exclusion from
    deleting files that are the source of truth (skills, opt-in workflows,
    bundled configs).

    ```{note}
    Detection is based on `output_dir` contents, not on `__file__`,
    because `uvx --from .` installs the package into a temp venv where
    `__file__` no longer points to the source checkout.
    ```
    """
    resolved = output_dir.resolve()
    return (resolved / "repomatic" / "__init__.py").exists() and (
        resolved / "repomatic" / "data"
    ).is_dir()


def _resolve_target(component_name: str, target: str, config: Config | None) -> str:
    """Rebase a target path onto its component's configured location.

    A thin lookup in front of {meth}`~repomatic.registry.Component.resolve_target`,
    for the callers that hold a component *name* (a `[tool.repomatic] exclude`
    entry, a {class}`~repomatic.registry.RemovedAsset` tombstone) rather than the
    component itself. Unknown names pass through untouched.
    """
    component = COMPONENTS_BY_NAME.get(component_name)
    if component is None:
        return target
    return component.resolve_target(target, config)


def _detect_removed_assets(
    output_dir: Path, config: Config | None
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Find on-disk orphans of assets repomatic has dropped.

    Walks {data}`~repomatic.registry.REMOVED_ASSETS`, resolves each tombstone's
    target through the same path overrides as live files, and classifies any
    that still exist on disk:

    - *prunable*: an untouched copy, safe to delete. For skills and agents the
      normalized content matches one of the recorded shipped hashes; for
      workflows the file is a repomatic-lineage thin-caller for the removed
      workflow with no extra downstream jobs.
    - *review*: present but customized locally (content differs, or a
      thin-caller carries extra jobs). Reported, never deleted.

    A file that is not recognizable as a repomatic-shipped orphan (a user's own
    workflow that merely shares a name) is left untouched.

    A tombstoned file that is already gone is not the end of it: an asset that
    shipped as a folder leaves that folder standing, and an empty one is
    prunable on its own. See {func}`_empty_owned_dir`.

    :param output_dir: Root directory of the target repository.
    :param config: Repomatic config for `skills.location` / `agents.location`
        path overrides.
    :return: `(prunable, review)`, each a list of `(relative_path,
        successor)` tuples sorted by path.
    """
    prunable: list[tuple[str, str]] = []
    review: list[tuple[str, str]] = []
    for asset in REMOVED_ASSETS:
        rel = _resolve_target(asset.component, asset.target, config)
        path = output_dir / rel
        if not path.exists():
            orphan_dir = _empty_owned_dir(asset, output_dir, config)
            if orphan_dir:
                prunable.append((orphan_dir, asset.successor))
            continue
        entry = (rel, asset.successor)
        if asset.component == "workflows":
            verdict = _classify_removed_workflow(path)
            if verdict == "prune":
                prunable.append(entry)
            elif verdict == "review":
                review.append(entry)
            # "skip": not a lineage thin-caller, leave it untouched.
        else:
            # Compare against the on-disk form init itself writes: rstrip()ed
            # with a single trailing newline. A match against any released
            # revision means the copy is untouched and safe to prune.
            normalized = path.read_text(encoding="UTF-8").rstrip() + "\n"
            digest = hashlib.sha256(normalized.encode("UTF-8")).hexdigest()
            if digest in asset.hashes:
                prunable.append(entry)
            else:
                review.append(entry)
    prunable.sort()
    review.sort()
    return prunable, review


def _empty_owned_dir(
    asset: RemovedAsset, output_dir: Path, config: Config | None
) -> str:
    """The folder a removed asset left behind empty, when there is one.

    A tombstone addresses a file, so it stops firing the moment that file goes
    by any route other than a full `init` prune: a hand `rm`, or a repomatic old
    enough to unlink the file without sweeping its parent. An asset that shipped
    as a folder then leaves that folder standing, empty, and invisible to every
    later run of a check keyed on the file. Pruning it needs no content gate: an
    empty directory holds nothing a repository could lose.

    A folder that still holds files is left alone. Whatever is in it, repomatic
    did not write it: its own entry point is the file already established as
    missing.

    :return: The folder's path relative to *output_dir*, or an empty string when
        the asset owned no folder, the folder is gone too, or it is not empty.
    """
    if not asset.owned_dir:
        return ""
    rel = _resolve_target(asset.component, asset.owned_dir, config)
    path = output_dir / rel
    if not path.is_dir() or any(path.iterdir()):
        return ""
    return rel


def _classify_removed_workflow(path: Path) -> str:
    """Classify an on-disk workflow file as an orphan of a removed workflow.

    Thin-callers are parameterized per repo (version pin, `paths:` filters), so
    they carry no fixed content to hash. The file is matched instead by its
    `uses:` fingerprint against the upstream slugs in
    {data}`~repomatic.registry.UPSTREAM_REPO_SLUGS`.

    :return: `"prune"` for a pure repomatic-lineage thin-caller for this
        workflow, `"review"` when the user appended extra jobs, or `"skip"`
        when the file is not a lineage thin-caller (leave it untouched).
    """
    for slug in UPSTREAM_REPO_SLUGS:
        if identify_canonical_workflow(path, slug) == path.name:
            extra = extract_extra_jobs(path.read_text(encoding="UTF-8"), slug)
            return "review" if extra.strip() else "prune"
    return "skip"


def _init_config_files(
    output_dir: Path,
    component_name: str,
    result: InitResult,
    *,
    exclude_ids: frozenset[str] = frozenset(),
    include_ids: frozenset[str] | None = None,
    config: Config | None = None,
) -> None:
    """Export bundled config files for a component.

    For components without `keep_unmodified`, files already on disk that
    are identical to the bundled template are flagged as unmodified and not
    overwritten. An {attr}`~repomatic.registry.Component.ephemeral` component is
    exempt: its files are scratch input a consumer regenerates before reading,
    so finding one that matches the bundled default is the expected steady state
    after the previous run, not drift worth reporting.

    :param exclude_ids: File identifiers to skip within this component.
    :param include_ids: When not `None`, only export files in this set.
    :param config: Repomatic config for path overrides (like `skills.location`
        or `agents.location`).
    """
    comp = COMPONENTS_BY_NAME[component_name]
    for entry in comp.files:
        if include_ids is not None and entry.file_id not in include_ids:
            continue
        if exclude_ids and entry.file_id in exclude_ids:
            continue
        target = output_dir / comp.resolve_target(entry.target, config)

        # A tree entry is a whole folder (a skill and its optional `scripts/`,
        # `references/` and `assets/`), copied verbatim rather than rendered.
        if entry.tree:
            source_root = files("repomatic.data").joinpath(entry.source)
            tree_created, tree_updated = _copy_template_tree(
                source_root, target, output_dir=output_dir
            )
            result.created.extend(
                p.relative_to(output_dir).as_posix() for p in tree_created
            )
            result.updated.extend(
                p.relative_to(output_dir).as_posix() for p in tree_updated
            )
            continue

        content = export_content(entry.source)
        outcome = _write_managed(target, content, result, output_dir)
        if outcome == "unchanged" and not comp.keep_unmodified and not comp.ephemeral:
            rel = target.relative_to(output_dir).as_posix()
            result.unmodified_configs.append(rel)
            logging.info(f"Unmodified (matches bundled default): {rel}")


AWESOME_TEMPLATE_SLUG = "kdeldycke/awesome-template"
"""Source slug embedded in bundled awesome-template files, rewritten at sync time."""


def _copy_template_tree(
    root: Traversable, dest: Path, *, output_dir: Path | None = None
) -> tuple[list[Path], list[Path]]:
    """Recursively copy files from a traversable resource tree to disk.

    Skips `__init__.py` and `__pycache__` entries, and leaves a file whose
    content already matches untouched, so re-running is a no-op.

    Byte-based rather than routed through {func}`_write_managed`: a template
    tree can carry binary assets (images), which a text round-trip would
    corrupt. The created/updated classification is the same, so the two stay
    worth reading side by side.

    :param output_dir: Repository root the log lines are made relative to. Left
        unset, paths are logged as-is, which is what a caller holding no
        repository root (a test) wants.
    :return: `(created, updated)` lists of the paths actually written.
    """
    created: list[Path] = []
    updated: list[Path] = []
    for entry in root.iterdir():
        if entry.name in ("__init__.py", "__pycache__"):
            continue
        if entry.is_dir():
            c, u = _copy_template_tree(entry, dest / entry.name, output_dir=output_dir)
            created.extend(c)
            updated.extend(u)
        else:
            target = dest / entry.name
            existed = target.exists()
            new_bytes = entry.read_bytes()
            if existed and target.read_bytes() == new_bytes:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new_bytes)
            label = _relative_label(target, output_dir)
            if existed:
                updated.append(target)
                logging.info(f"Updated: {label}")
            else:
                created.append(target)
                logging.info(f"Created: {label}")
    return created, updated


def init_awesome_template(
    output_dir: Path,
    repo_slug: str,
    result: InitResult,
) -> None:
    """Copy bundled awesome-template files and rewrite URLs.

    Copies all files from the `repomatic/data/awesome_template/` bundle into
    *output_dir* and rewrites `kdeldycke/awesome-template` URLs in
    `.github/` markdown and YAML files to match *repo_slug*.

    Every copied file is recorded on *result* by its own relative path, the way
    the skills and agents trees already are. The roll-up stays a log line: those
    lists are consumed as paths (`--delete-excluded` joins them against
    *output_dir*), so a `"awesome-template (12 files)"` summary sitting among
    them would be a path that resolves nowhere.

    :param output_dir: Root directory of the target repository.
    :param repo_slug: Target `owner/name` slug for URL rewriting.
    :param result: {class}`InitResult` accumulator for created/updated files.
    """
    template_root = files("repomatic.data").joinpath("awesome_template")
    created, updated = _copy_template_tree(
        template_root, output_dir, output_dir=output_dir
    )
    result.created.extend(p.relative_to(output_dir).as_posix() for p in created)
    result.updated.extend(p.relative_to(output_dir).as_posix() for p in updated)
    if created or updated:
        logging.info(
            f"awesome-template: {len(created)} file(s) created, {len(updated)} updated."
        )

    # Rewrite template URLs in .github/ markdown and YAML files.
    github_dir = output_dir / ".github"
    if github_dir.is_dir():
        for path in github_dir.rglob("*"):
            if not path.is_file() or path.suffix not in (".md", ".yaml", ".yml"):
                continue
            content = path.read_text(encoding="UTF-8")
            new_content = content.replace(
                f"/{AWESOME_TEMPLATE_SLUG}/", f"/{repo_slug}/"
            )
            if new_content != content:
                path.write_text(new_content, encoding="UTF-8")
                logging.info(f"Rewrote URLs in: {path}")


def _init_changelog(
    output_dir: Path,
    result: InitResult,
    *,
    config: Config | None = None,
) -> None:
    """Create a minimal changelog.md if it doesn't exist.

    The changelog stub is only useful for bootstrapping new repositories.
    An existing `changelog.md` is never overwritten — it contains real
    release history that would be destroyed by the stub template.
    """
    location = location_path((config or Config()).changelog_location)
    changelog_path = output_dir / location
    # Guarded before writing rather than left to `_write_managed`: that helper
    # converges a file onto the given content, which for a changelog would
    # replace real release history with the stub.
    if changelog_path.exists():
        rel = changelog_path.relative_to(output_dir).as_posix()
        result.skipped.append(rel)
        logging.debug(f"Skipped existing: {rel}")
        return
    changelog_content = (
        "# Changelog\n"
        "\n"
        "## [Unreleased](https://github.com/USER/REPO/compare/main...main)\n"
    )
    _write_managed(changelog_path, changelog_content, result, output_dir)


def _init_plugin_settings(
    output_dir: Path,
    result: InitResult,
    *,
    config: Config | None = None,
) -> None:
    """Wire the repomatic Claude Code plugin into the project settings.

    Unlike the changelog stub, an existing file is merged into rather than
    skipped: it is a live settings document the repository keeps editing, and
    only the marketplace and enablement keys the plugin owns are touched. That
    also makes a re-run a no-op, reported as unchanged.
    """
    location = location_path((config or Config()).settings_location)
    settings_path = output_dir / location
    rel = settings_path.relative_to(output_dir).as_posix()
    existed = settings_path.is_file()
    if not merge_plugin_settings(settings_path, output_dir):
        logging.debug(f"Unchanged: {rel}")
        return
    if existed:
        result.updated.append(rel)
        logging.info(f"Updated: {rel}")
    else:
        result.created.append(rel)
        logging.info(f"Created: {rel}")


def _fetch_extra_labels(
    output_dir: Path,
    result: InitResult,
    config: Config | None = None,
) -> None:
    """Download extra label files from `[tool.repomatic]` config.

    Reads `labels.extra-files` URLs and downloads each file to an
    `extra-labels/` subdirectory under `output_dir`.
    Does nothing if no URLs are configured.

    Fetched through the shared {func}`~repomatic.http.get_bytes` seam, so the
    download carries the standard timeout, `User-Agent` and truncation retry.
    These URLs come from a downstream repository's own config and are reached
    on every labels sync, so an unresponsive host has to fail the step rather
    than hang the job.
    """
    if config is None:
        config = load_repomatic_config()
    urls = config.labels.extra_files
    if not urls:
        logging.debug("No labels.extra-files configured.")
        return

    target_dir = output_dir / "extra-labels"
    target_dir.mkdir(exist_ok=True)
    for url in urls:
        url = url.strip()
        if not url:
            continue
        filename = PurePosixPath(url).name
        target = target_dir / filename
        rel = target.relative_to(output_dir).as_posix()
        logging.info(f"Downloading {url} -> {target}")
        target.write_bytes(get_bytes(url))
        result.created.append(rel)


def _init_tool_configs(
    output_dir: Path,
    tool_configs: Sequence[str],
    result: InitResult,
) -> None:
    """Merge selected tool configs into pyproject.toml."""
    pyproject_path = output_dir / "pyproject.toml"
    if not pyproject_path.exists():
        result.warnings.append(
            "pyproject.toml not found; skipping tool config initialization."
        )
        logging.warning(result.warnings[-1])
        return
    rel = pyproject_path.relative_to(output_dir).as_posix()
    for config_type in tool_configs:
        tc = COMPONENTS_BY_NAME[config_type]
        assert isinstance(tc, ToolConfigComponent)
        section = tc.tool_section
        had_section = re.search(
            rf"^\[{re.escape(section)}\]",
            pyproject_path.read_text(encoding="UTF-8"),
            re.MULTILINE,
        )
        merged = init_config(config_type, pyproject_path)
        if merged is None:
            logging.info(f"[{section}] already up to date, skipped.")
        else:
            pyproject_path.write_text(merged, encoding="UTF-8")
            if had_section:
                logging.info(f"Updated [{section}].")
                if rel not in result.updated:
                    result.updated.append(rel)
            else:
                logging.info(f"Merged [{section}].")
                if rel not in result.created:
                    result.created.append(rel)
