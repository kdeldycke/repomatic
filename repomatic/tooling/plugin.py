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

"""Distribution of the bundled skills and agents as a Claude Code plugin.

Two halves of the same story, kept together because they share the plugin's
identity constants:

- {func}`pack_plugin` assembles the zip the release engine attaches to every
  GitHub release, from the manifest and asset directories already in the tree.
- {func}`merge_plugin_settings` writes the marketplace and enablement wiring
  into a consumer's Claude Code settings, so a downstream repository can install
  the plugin instead of carrying copied skill files.

```{caution}
Every asset sits at the spec's default `skills/` and `agents/` location *within*
{data}`PLUGIN_ROOT`, and the manifest therefore declares no component paths at
all.

That layout is not a stylistic choice. A manifest naming individual agent
files (`"agents": ["./.claude/agents/qa-engineer.md", ...]`, the only form the
[published
schema](https://json.schemastore.org/claude-code-plugin-manifest.json) accepts,
since it constrains the field to paths ending in `.md`) passes `claude plugin
validate --strict` and then loads **zero** agents at runtime, silently. Naming
the directory instead fails validation outright. The default location is the only
shape that actually works, verified against Claude Code 2.1.220 by loading the
packed archive and counting components with `claude plugin details`. `skills`
does honor a custom directory, but there is no reason to keep one half on the
mechanism that misbehaves, so both stay at their defaults and the manifest
remains metadata-only.
```

```{note}
{data}`PLUGIN_ROOT` is the single source of truth, and it is directly
installable: `claude --plugin-dir .claude` loads the same components a consumer
gets, with no packing step in between. {func}`pack_plugin` mirrors that tree into
the archive rather than rearranging it, so there is no symlink anywhere and no
second copy of any skill.
```

```{note}
Three copies of the version travel with this plugin, and none is maintained by
hand. {func}`pack_plugin` injects the running `__version__` into the manifest it
writes to the archive, while the checked-in manifest and the catalog entry are
both stamped by the release freeze
({meth}`.PrepareRelease.freeze_plugin_manifest_version` and
{meth}`.PrepareRelease.freeze_marketplace_pin`).

Claude Code compares that string against a user's installed copy to decide
whether an update is due, so a value that went stale would silently strand
everyone on the plugin they already had. Deriving each one keeps the single
repomatic-specific `[[tool.bumpversion.files]]` entry out of a
`[tool.bumpversion]` block that `sync-bumpversion` regenerates from a bundled
template shared with every downstream repository, and keeps the post-release
`.devN` bump from advertising a release that does not exist.
```

```{note}
The marketplace entry is a `git-subdir` source naming {data}`PLUGIN_ROOT` in this
repository, and its two halves move differently.
{meth}`.PrepareRelease.freeze_marketplace_pin` writes both on a release commit;
{meth}`.PrepareRelease.unfreeze_marketplace_ref` then walks the `ref` back to the
default branch and leaves the `version` on the release. So a released tree
installs exactly that release, while the default branch tracks itself, which is
the only state the app's `Sync automatically` can ever observe a change in.

That split is why the tag is not simply pinned everywhere: a tag never moves, so
an entry frozen to one turns automatic sync into a no-op until the next release.
It also survives a layout change, where a pin naming the last release cannot: the
plugin root moved into {data}`PLUGIN_ROOT` in this very cycle, and a catalog
pinned to the previous tag pointed at a tree with no manifest in it.

An `archive` source pointing at the release asset was the previous shape, and it
worked in the CLI alone. The claude.ai ingester behind the Desktop and Cowork
plugin panels rejects it with `External plugin source type 'archive' is not
supported. Supported types: git-subdir, github, url`, surfaced in the app as a
bare "Marketplace sync failed" naming no cause. `git-subdir` is the narrowest
supported type: it clones this one directory through a partial clone rather than
the whole repository.
```

```{caution}
The entry pins a tag through `ref`, not a commit through `sha`, so it inherits
whatever the tag points at. A tag is movable and a `sha` is not, which is the
stronger guarantee of the two; the tag is kept because it is what
{meth}`.PrepareRelease.freeze_marketplace_pin` can write from the version it is
already freezing, where a SHA is only knowable after the freeze commit exists.

The release asset is no longer what a marketplace install fetches, so a release
publishing without it leaves `/plugin install` working. It still strands the
manual Desktop and Cowork upload, the only route for anyone not syncing this
catalog, which is why a failed `extra-assets` keeps blocking `publish-release`.
```
"""

from __future__ import annotations

import json
import logging
import zipfile
from collections.abc import Mapping
from pathlib import Path

from .. import __version__
from ..pyproject import read_pyproject_toml
from ..registry import COMPONENTS_BY_NAME
from .tool_registry import TOOL_REGISTRY

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any, Final

PLUGIN_ROOT = ".claude"
"""Directory that *is* the plugin, relative to the repository root.

It already holds `skills/` and `agents/` at the locations the plugin spec scans,
so naming it as the marketplace entry's `path` publishes the tree as-is.

```{caution}
The whole directory is what a consumer clones, which is wider than the archive:
{func}`pack_plugin` admits only what the component registry declares, while a
`git-subdir` install carries every file sitting here. Anything added under this
path ships to every consumer, so keep it to the components themselves.
```
"""

MANIFEST_PATH = ".claude-plugin/plugin.json"
"""Location of the plugin manifest, relative to the plugin root.

The same path in both places it appears: under {data}`PLUGIN_ROOT` in the tree,
where {func}`pack_plugin` reads it and a marketplace install finds it, and at the
top of the archive, where Claude Code looks for it. Join it onto the repository
root through {data}`REPO_MANIFEST_PATH`, never on its own.
"""

REPO_MANIFEST_PATH = f"{PLUGIN_ROOT}/{MANIFEST_PATH}"
"""Location of the plugin manifest, relative to the repository root."""

MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
"""Location of the marketplace catalog, relative to the repository root."""

PLUGIN_NAME = "repomatic"
"""The plugin's `name`, which namespaces every skill and agent it ships.

Users type it as `/plugin install repomatic@kdeldycke` and see it in the scoped
component names (`repomatic:qa-engineer`). Renaming it breaks every existing
install, so it lives here as a constant and is asserted against the manifest
rather than read from it.
"""

MARKETPLACE_NAME = "kdeldycke"
"""The marketplace's `name`, the catalog this plugin is published in.

Named after the owner rather than the project, so sibling repositories can be
listed in the same catalog later. Like {data}`PLUGIN_NAME`, renaming it breaks
every existing install.
"""

MARKETPLACE_REPO = "kdeldycke/repomatic"
"""Repository a consumer registers to reach {data}`MARKETPLACE_PATH`."""

BIOME_DEFAULT_INDENT: Final[str] = "\t"
"""Indent `format-json` writes when no Biome configuration overrides it.

Biome's own default, so a repository declaring nothing gets a rendered document
the formatter already agrees with.
"""

BIOME_DEFAULT_INDENT_WIDTH: Final[int] = 2
"""Spaces per level Biome assumes when a config asks for spaces without a width."""

ARCHIVE_NAME = "repomatic-claude-plugin.zip"
"""Filename of the release asset {func}`pack_plugin` produces.

Carries `claude` because a bare `repomatic-plugin.zip` reads backwards: packaging
names an extension after its host first (`pytest-cov`, `mdformat-gfm`), so that
filename announces a plugin *for* repomatic on a release page, which is also what
"plugin" means for the mdformat entries of
{mod}`~repomatic.tooling.tool_registry`. The name mirrors the spec's own
`.claude-plugin/` directory instead.

Also the default `--output` of `repomatic pack-plugin`, so the release job never
spells it. It still appears in `[tool.repomatic] release-assets` and in the
`release-asset-` run-artifact name the engine matches, which TOML and YAML cannot
read from here; `tests/test_workflows.py` holds all three equal. Nothing else
names it: the marketplace stopped pointing at the asset when it moved to a
`git-subdir` source, so renaming it now reaches only the upload instructions.
"""

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
"""Fixed modification time stamped on every archive member.

The earliest timestamp the ZIP format can represent. Together with a sorted
member list and an explicit file mode, it makes {func}`pack_plugin`
byte-deterministic, so re-packing an unchanged tree yields an identical archive.
That matters more here than it usually would: with no `sha256` pin in the
marketplace entry, the archive's own digest is what Claude Code falls back to
for change detection.
"""

FILE_MODE = 0o644
"""Permission bits stamped on every archive member.

Stamped explicitly rather than copied from disk so the archive does not vary
with the packing runner's umask.
"""

AGENTS_DIR = "agents"
"""Directory the plugin spec scans for agent definitions, inside the plugin root."""

SKILLS_DIR = "skills"
"""Directory the plugin spec scans for skill folders, inside the plugin root."""


def _plugin_assets(repo_root: Path) -> Iterator[tuple[Path, Path]]:
    """Pair every asset the plugin ships with its path inside the archive.

    Walks the `subagents` and `skills` component registries rather than globbing
    `.claude/`, so a file the registry does not declare cannot ride along into a
    published archive. Skill entries are whole folders, which is what picks up a
    skill's optional `references/`, `scripts/` and `assets/` subdirectories once
    one grows them.

    Each asset is placed under the plugin spec's default {data}`AGENTS_DIR` and
    {data}`SKILLS_DIR`, for the reason the module docstring gives: a manifest
    pointing at a custom agents path loads nothing. That is where
    {data}`PLUGIN_ROOT` already holds them, so the mapping is an identity for this
    repository; it is written out rather than derived, because a downstream
    `[tool.repomatic] skills.location` may move the source without moving where
    the spec scans.

    :param repo_root: Repository root.
    :return: Iterator of `(source relative to repo_root, path inside the plugin)`.
    :raises FileNotFoundError: If a declared agent file or skill folder is absent.
    """
    for entry in COMPONENTS_BY_NAME["subagents"].files:
        source = Path(entry.target)
        if not (repo_root / source).is_file():
            msg = f"Subagent file {entry.target} is missing."
            raise FileNotFoundError(msg)
        yield source, Path(AGENTS_DIR) / source.name

    for entry in COMPONENTS_BY_NAME["skills"].files:
        skill_dir = Path(entry.target)
        if not (repo_root / skill_dir).is_dir():
            msg = f"Skill folder {entry.target} is missing."
            raise FileNotFoundError(msg)
        for path in sorted((repo_root / skill_dir).rglob("*")):
            if path.is_file():
                source = path.relative_to(repo_root)
                # `entry.target` is `<skills location>/<skill id>`, so keeping the
                # tail below it carries a skill's own subdirectories along.
                tail = source.relative_to(skill_dir)
                yield source, Path(SKILLS_DIR) / skill_dir.name / tail


def _stamped_manifest(repo_root: Path, version: str = __version__) -> bytes:
    """Read the manifest and stamp the packaged version into it.

    :param repo_root: Repository root holding {data}`PLUGIN_ROOT`.
    :param version: Version string to stamp.
    :return: The manifest as UTF-8 JSON bytes, ready to write into the archive.
    :raises FileNotFoundError: If the manifest is absent.
    :raises TypeError: If the manifest is not a JSON object.
    """
    manifest_path = repo_root / REPO_MANIFEST_PATH
    if not manifest_path.is_file():
        msg = f"Plugin manifest {REPO_MANIFEST_PATH} is missing."
        raise FileNotFoundError(msg)

    manifest = json.loads(manifest_path.read_text(encoding="UTF-8"))
    if not isinstance(manifest, dict):
        msg = f"{REPO_MANIFEST_PATH} must be a JSON object."
        raise TypeError(msg)

    manifest["version"] = version
    # Sorted keys and a tab indent, so the packaged manifest reads exactly like
    # the checked-in one `format-json` maintains.
    return (json.dumps(manifest, indent="\t", sort_keys=True) + "\n").encode("UTF-8")


def pack_plugin(repo_root: Path, output: Path, version: str = __version__) -> list[str]:
    """Pack the manifest and its assets into an installable plugin archive.

    The archive holds a single top-level folder named after the plugin. That is
    one of the two layouts Claude Code accepts, and the one that makes `unzip &&
    claude --plugin-dir repomatic` work on the downloaded asset. Inside it, the
    layout mirrors {data}`PLUGIN_ROOT`, minus everything the component registry
    does not declare: see the module docstring for why.

    :param repo_root: Repository root the assets are read from.
    :param output: Destination `.zip` path. Parent directories are created.
    :param version: Version stamped into the packaged manifest.
    :return: Archive member names, sorted.
    :raises FileNotFoundError: If the manifest, an agent file or a skill folder
        is missing.
    :raises TypeError: If the manifest is not a JSON object.
    """
    payloads = {Path(MANIFEST_PATH): _stamped_manifest(repo_root, version)}
    for source, member in _plugin_assets(repo_root):
        payloads[member] = (repo_root / source).read_bytes()

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(payloads):
            info = zipfile.ZipInfo(f"{PLUGIN_NAME}/{member.as_posix()}")
            info.date_time = ZIP_TIMESTAMP
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_MODE << 16
            archive.writestr(info, payloads[member])

    names = [f"{PLUGIN_NAME}/{member.as_posix()}" for member in sorted(payloads)]
    logging.info(f"Packed {len(names)} files into {output}")
    return names


def _plugin_settings() -> dict[str, dict[str, object]]:
    """Build the settings fragment that enables the plugin for a repository.

    Two independent keys, both required: `extraKnownMarketplaces` registers the
    catalog, and `enabledPlugins` turns this plugin on within it. Declaring them
    in a project's `.claude/settings.json` prompts each collaborator to install
    the plugin once they trust the folder. Claude Code never installs it on their
    behalf, which is why the copied-file `skills` and `agents` components stay in
    place alongside this rather than being replaced by it.

    :return: The fragment to merge into a Claude Code settings document.
    """
    return {
        "enabledPlugins": {f"{PLUGIN_NAME}@{MARKETPLACE_NAME}": True},
        "extraKnownMarketplaces": {
            MARKETPLACE_NAME: {
                "source": {"repo": MARKETPLACE_REPO, "source": "github"},
            },
        },
    }


def _biome_formatter_tables(config: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield Biome's formatter tables from *config*, least specific first.

    The global `formatter` table sets the baseline and the per-language
    `json.formatter` one narrows it, so a caller folding them in this order
    ends up with the settings Biome itself would apply to a `.json` file.

    :param config: A parsed Biome configuration document.
    :return: Iterator over the formatter tables actually present.
    """
    for path in (("formatter",), ("json", "formatter")):
        table: Any = config
        for key in path:
            table = table.get(key) if isinstance(table, Mapping) else None
        if isinstance(table, Mapping):
            yield table


def _biome_json_indent(root: Path) -> str:
    """Derive the JSON indent `format-json` imposes on *root*'s files.

    Mirrors {func}`~repomatic.tooling.tool_runner.resolve_config`'s precedence: a native
    `biome.json` replaces `[tool.biome]` in `pyproject.toml` rather than layering
    over it.

    ```{caution}
    Anything unreadable (a `biome.jsonc` whose comments no JSON parser accepts, a
    malformed table) falls back to {data}`BIOME_DEFAULT_INDENT` instead of
    raising. Guessing the style wrong costs one reformat that `format-json` then
    corrects; raising would take down the whole `repomatic init`.
    ```

    :param root: Repository root holding the Biome configuration.
    :return: The literal string one level of indentation is written with.
    """
    config: Mapping[str, Any] = {}
    # Probe every filename the registry declares for Biome, in its order: a
    # repository configured through `biome.jsonc` used to fall through to the
    # `[tool.biome]` branch and could get an indent `format-json` then fought.
    native = next(
        (
            candidate
            for name in TOOL_REGISTRY["biome"].native_config_files
            if (candidate := root / name).is_file()
        ),
        None,
    )
    if native is not None:
        try:
            loaded = json.loads(native.read_text(encoding="UTF-8"))
        except (OSError, ValueError):
            logging.debug(f"Unreadable {native}, assuming Biome's default indent.")
            return BIOME_DEFAULT_INDENT
        if isinstance(loaded, Mapping):
            config = loaded
    else:
        tool = read_pyproject_toml(root).get("tool", {})
        if isinstance(tool, Mapping) and isinstance(tool.get("biome"), Mapping):
            config = tool["biome"]

    style: Any = None
    width: Any = None
    for table in _biome_formatter_tables(config):
        style = table.get("indentStyle", style)
        width = table.get("indentWidth", width)

    if style != "space":
        return BIOME_DEFAULT_INDENT
    if not isinstance(width, int) or isinstance(width, bool) or width < 1:
        return " " * BIOME_DEFAULT_INDENT_WIDTH
    return " " * width


def render_plugin_settings(
    existing: str = "",
    indent: str = BIOME_DEFAULT_INDENT,
) -> str:
    """Merge the plugin wiring into an existing settings document.

    Only the two keys {func}`_plugin_settings` owns are touched, and within them
    only the entries this plugin and marketplace are named by: a repository's own
    permissions, hooks and any unrelated marketplace survive untouched.

    Sorted keys, and *indent* whichever way `format-json` writes JSON in the
    consuming repository, so writing the file leaves no drift for the formatter
    to raise a pull request about. Biome preserves key order, which is why only
    the indent has to be negotiated.

    :param existing: Current file content, or an empty string when absent.
    :param indent: One level of indentation, from {func}`_biome_json_indent`.
    :return: The merged document, newline-terminated.
    :raises TypeError: If *existing* is not a JSON object.
    """
    document: dict[str, object] = {}
    if existing.strip():
        loaded = json.loads(existing)
        if not isinstance(loaded, dict):
            msg = "Claude Code settings must be a JSON object."
            raise TypeError(msg)
        document = loaded

    for key, entries in _plugin_settings().items():
        section = document.get(key)
        # A non-object value is someone else's mistake, not something to merge
        # into: replace it rather than raising, so init still converges.
        merged = dict(section) if isinstance(section, dict) else {}
        merged.update(entries)
        document[key] = merged

    return json.dumps(document, indent=indent, sort_keys=True) + "\n"


def merge_plugin_settings(target: Path, root: Path | None = None) -> bool:
    """Write the plugin wiring into *target*, creating the file if absent.

    Idempotent: re-running against an already-wired document rewrites nothing
    and returns `False`, so `repomatic init` reports it as unchanged. The
    comparison is made on the parsed documents, not the rendered text, because
    the two serializers disagree on more than the indent *root* negotiates:
    {func}`json.dumps` expands every array one item per line, while Biome
    collapses one that fits its line width. Comparing the text reported drift on
    a file `format-json` had already settled, and the two rewrote it past each
    other on every run, each opening a pull request undoing the other's.

    A first write still lands in this module's own style, which `format-json`
    then settles once. That is the same one-reformat cost
    {func}`_biome_json_indent` accepts when it has to guess.

    :param target: Path to the Claude Code settings file to update.
    :param root: Repository root whose Biome config sets the indent. Defaults to
        *target*'s own directory, which is right only for a root-level file.
    :return: Whether the file was created or modified.
    """
    existing = target.read_text(encoding="UTF-8") if target.is_file() else ""
    merged = render_plugin_settings(
        existing, _biome_json_indent(root if root is not None else target.parent)
    )
    # `existing` parsed cleanly above, or `render_plugin_settings` would have
    # raised, so only the empty-file case needs guarding here.
    if existing.strip() and json.loads(existing) == json.loads(merged):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(merged, encoding="UTF-8")
    return True
